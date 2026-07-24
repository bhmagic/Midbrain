from __future__ import annotations

import asyncio
import json
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from .artifacts import MonitorArtifacts
from .config import Settings, load_skill_config
from .models import RunMode
from .scene import WorldPointCloud
from .skill import AlignmentSkill


class RunRequest(BaseModel):
    mode: RunMode = RunMode.AUTO
    arm_is_home: bool = False
    allow_active_control_interrupt: bool = False


settings = Settings()
config = load_skill_config()
artifacts = MonitorArtifacts()
skill = AlignmentSkill(settings=settings, config=config, artifacts=artifacts)
cloud = WorldPointCloud(
    skill.fabric,
    config["camera_frame"],
    stride=int(config["gui"]["point_cloud_stride"]),
    update_hz=float(config["gui"]["point_cloud_hz"]),
    max_points=int(config["gui"]["point_cloud_max_points"]),
)
WEB_ROOT = Path(__file__).resolve().parent / "web"
GUI_PID_PATH = settings.run_root / "gui.pid.json"
provider_task: asyncio.Task[dict[str, Any]] | None = None


def _consume_task(task: asyncio.Task[Any]) -> None:
    try:
        task.result()
    except (asyncio.CancelledError, Exception):
        pass


@asynccontextmanager
async def lifespan(_: FastAPI):
    global provider_task
    GUI_PID_PATH.write_text(
        json.dumps(
            {
                "gui": os.getpid(),
                "started_at_us": time.time_ns() // 1000,
                "owner": "stationary_world_arm_alignment.app",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    await cloud.start()
    provider_task = asyncio.create_task(
        skill.request_runtime_inputs(wait_for_vio_tracking=False),
        name="stationary-world-provider-bootstrap",
    )
    provider_task.add_done_callback(_consume_task)
    try:
        yield
    finally:
        if provider_task and not provider_task.done():
            provider_task.cancel()
            try:
                await provider_task
            except (asyncio.CancelledError, Exception):
                pass
        if skill.current_task and not skill.current_task.done():
            await skill.cancel()
            try:
                await skill.current_task
            except (asyncio.CancelledError, Exception):
                pass
        await cloud.stop()
        await skill.close()
        try:
            record = json.loads(GUI_PID_PATH.read_text(encoding="utf-8"))
            if int(record.get("gui", -1)) == os.getpid():
                GUI_PID_PATH.unlink(missing_ok=True)
        except Exception:
            pass


app = FastAPI(title="Stationary World-Space Arm Finder", lifespan=lifespan)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB_ROOT / "index.html")


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "stationary-world-arm-alignment"})


@app.get("/assets/{name}")
async def asset(name: str) -> FileResponse:
    if name not in {"app.js", "style.css"}:
        raise HTTPException(404)
    return FileResponse(WEB_ROOT / name)


@app.get("/api/status")
async def status() -> JSONResponse:
    progress, cloud_status, runtime = await asyncio.gather(
        skill.progress.snapshot(),
        cloud.status(),
        skill.runtime_snapshot(),
    )
    foundation_id = config["foundation_pose_provider_id"]
    foundation_view = (runtime.get("providers") or {}).get(foundation_id) or {}
    if str(foundation_view.get("process_state", "")).lower() in {"running", "starting"}:
        try:
            foundation = await skill.foundation_health.health()
        except Exception as error:
            foundation = {"reachable": False, "error": str(error)}
    else:
        foundation = {
            "reachable": False,
            "state": "STOPPED_ON_DEMAND",
            "message": "FoundationPose starts only for base alignment.",
        }
    return JSONResponse(
        {
            "progress": progress,
            "runtime": runtime,
            "point_cloud": cloud_status,
            "foundation_pose": foundation,
            "latest_calibration": skill.store.latest(),
        }
    )


@app.post("/api/providers/request")
async def request_providers() -> JSONResponse:
    global provider_task
    if provider_task is not None and not provider_task.done():
        return JSONResponse({"accepted": False, "reason": "provider request already active"})
    provider_task = asyncio.create_task(
        skill.request_runtime_inputs(wait_for_vio_tracking=False),
        name="stationary-world-provider-bootstrap",
    )
    provider_task.add_done_callback(_consume_task)
    return JSONResponse({"accepted": True})


@app.post("/api/run")
async def run(request: RunRequest) -> JSONResponse:
    try:
        task = skill.start(
            request.mode,
            arm_is_home=request.arm_is_home,
            allow_active_control_interrupt=request.allow_active_control_interrupt,
        )
        task.add_done_callback(_consume_task)
    except RuntimeError as error:
        raise HTTPException(409, detail=str(error)) from error
    return JSONResponse({"accepted": True, "mode": request.mode})


@app.post("/api/cancel")
async def cancel() -> JSONResponse:
    await skill.cancel()
    return JSONResponse({"accepted": True})


@app.post("/api/point-cloud/clear")
async def clear_point_cloud() -> JSONResponse:
    await cloud.clear()
    return JSONResponse({"cleared": True})


@app.get("/api/point-cloud")
async def point_cloud() -> Response:
    return Response(await cloud.snapshot_binary(), media_type="application/octet-stream")


@app.get("/api/geometry")
async def geometry() -> JSONResponse:
    return JSONResponse(await artifacts.geometry_snapshot())


@app.get("/api/image/{kind}")
async def image(kind: str) -> Response:
    if kind not in {"rgb", "depth", "overlay"}:
        raise HTTPException(404)
    payload = await artifacts.image(kind)
    if payload is None:
        raise HTTPException(404, detail=f"{kind} image has not been captured")
    mime = "image/png" if kind == "depth" else "image/jpeg"
    return Response(payload, media_type=mime, headers={"Cache-Control": "no-store"})


@app.get("/api/pose-overlay/{alignment_id}/{attempt}")
async def pose_overlay(alignment_id: str, attempt: int) -> FileResponse:
    if not re.fullmatch(r"[0-9A-Za-z-]+", alignment_id) or attempt not in {1, 2}:
        raise HTTPException(404)
    path = (
        settings.run_root
        / alignment_id
        / f"foundation_pose_attempt_{attempt}_overlay.jpg"
    )
    if not path.is_file():
        raise HTTPException(404, detail="pose overlay does not exist")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/api/calibrations")
async def calibrations() -> JSONResponse:
    return JSONResponse({"calibrations": skill.store.list()})


def main() -> None:
    uvicorn.run(
        "stationary_world_arm_alignment.app:app",
        host=str(config["gui"]["host"]),
        port=int(config["gui"]["port"]),
        reload=False,
    )


if __name__ == "__main__":
    main()
