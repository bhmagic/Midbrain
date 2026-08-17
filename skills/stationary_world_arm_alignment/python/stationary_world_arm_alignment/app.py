from __future__ import annotations

import asyncio
import json
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

import uvicorn
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from .artifacts import MonitorArtifacts
from .candidate_review import (
    CandidateReviewError,
    CandidateReviewService,
    ExternalReviewIdentityVerifier,
)
from .config import Settings, load_skill_config
from .models import RunMode
from .scene import WorldPointCloud
from .skill import AlignmentSkill
from .vlm import OPENAI_API_ROUTE


class RunRequest(BaseModel):
    mode: RunMode = RunMode.AUTO
    arm_is_home: bool = False
    allow_active_control_interrupt: bool = False
    vision_route: Literal["OPENAI_API", "REVIEWED_FILE"] = OPENAI_API_ROUTE
    review_timeout_s: float = Field(default=300.0, ge=1.0, le=900.0)


class CandidateDecisionRequest(BaseModel):
    decision: str
    candidate_sha256: str
    expected_provenance: dict[str, Any]
    idempotency_key: str
    rationale: str = ""


settings = Settings()
config = load_skill_config()
artifacts = MonitorArtifacts()
skill = AlignmentSkill(settings=settings, config=config, artifacts=artifacts)
candidate_reviews = CandidateReviewService(
    skill.store,
    settings.review_root,
)
review_identity = ExternalReviewIdentityVerifier()
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


def auto_bootstrap_providers_enabled() -> bool:
    return os.getenv("MIDBRAIN_GUI_AUTO_BOOTSTRAP_PROVIDERS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def persisted_alignment_image(
    kind: str,
    *,
    latest: dict[str, Any] | None,
    run_root: Path,
) -> bytes | None:
    """Read the latest persisted image without trusting stored local paths."""

    if kind not in {"rgb", "depth", "overlay"} or not isinstance(latest, dict):
        return None
    alignment_id = str(latest.get("alignment_id") or "")
    if re.fullmatch(r"[0-9A-Za-z-]+", alignment_id) is None:
        return None
    root = run_root.resolve()
    run_dir = (root / alignment_id).resolve()
    if run_dir.parent != root or not run_dir.is_dir():
        return None
    if kind == "overlay":
        selected = sorted(
            run_dir.glob("foundation_pose_attempt_*_selected_overlay.jpg")
        )
        candidates = [*reversed(selected), run_dir / "overlay.jpg"]
    elif kind == "rgb":
        candidates = [run_dir / "camera.jpg"]
    else:
        candidates = [run_dir / "depth.png"]
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.parent == run_dir and resolved.is_file():
            return resolved.read_bytes()
    return None


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
    if auto_bootstrap_providers_enabled():
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
    return JSONResponse(
        {
            "status": "ok",
            "service": "stationary-world-arm-alignment",
            "passive_gui_startup": not auto_bootstrap_providers_enabled(),
        }
    )


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
            vision_route=request.vision_route,
            review_timeout_s=request.review_timeout_s,
        )
        task.add_done_callback(_consume_task)
    except RuntimeError as error:
        raise HTTPException(409, detail=str(error)) from error
    return JSONResponse(
        {
            "accepted": True,
            "mode": request.mode,
            "vision_route": request.vision_route,
            "review_timeout_s": request.review_timeout_s,
        }
    )


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
    progress = await skill.progress.snapshot()
    if str(progress.get("state") or "") != "RUNNING":
        try:
            persisted = persisted_alignment_image(
                kind,
                latest=skill.store.latest(),
                run_root=settings.run_root,
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            persisted = None
        if persisted is not None:
            payload = persisted
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


@app.get("/api/candidate-reviews")
async def list_candidate_reviews() -> JSONResponse:
    return JSONResponse(
        {
            "identity_verification_available": review_identity.available,
            "activation_supported": False,
            "approval_executes_action": False,
            "candidates": candidate_reviews.list_candidates(),
        }
    )


@app.post("/api/candidate-reviews/{alignment_id}/decision")
async def decide_candidate(
    alignment_id: str,
    body: CandidateDecisionRequest,
    x_midbrain_review_assertion: str | None = Header(default=None),
) -> JSONResponse:
    result = skill.store.get(alignment_id)
    if result is None:
        raise HTTPException(
            404,
            detail={
                "code": "CANDIDATE_NOT_FOUND",
                "message": "calibration candidate does not exist",
            },
        )
    candidate = result.get("candidate") or {}
    decision = body.decision.strip().upper()
    try:
        identity = review_identity.verify(
            x_midbrain_review_assertion,
            candidate_id=str(candidate.get("candidate_id") or ""),
            candidate_sha256=body.candidate_sha256.lower(),
            decision=decision,
        )
        record, created = candidate_reviews.decide(
            alignment_id,
            body.model_dump(),
            verified_identity=identity,
        )
    except CandidateReviewError as error:
        if error.code == "IDENTITY_SERVICE_UNAVAILABLE":
            status_code = 503
        elif error.code in {
            "IDENTITY_ASSERTION_REQUIRED",
            "INVALID_IDENTITY_ASSERTION",
            "EXPIRED_IDENTITY_ASSERTION",
            "IDENTITY_SCOPE_MISMATCH",
            "VERIFIED_IDENTITY_REQUIRED",
        }:
            status_code = 401
        elif error.code == "CANDIDATE_NOT_FOUND":
            status_code = 404
        elif error.code in {
            "CANDIDATE_EXPIRED",
            "CANDIDATE_DIGEST_MISMATCH",
            "PROVENANCE_MISMATCH",
            "IDEMPOTENCY_CONFLICT",
            "CANDIDATE_ALREADY_REVIEWED",
        }:
            status_code = 409
        else:
            status_code = 422
        raise HTTPException(
            status_code,
            detail={"code": error.code, "message": str(error)},
        ) from error
    return JSONResponse(
        {
            "created": created,
            "decision": record,
            "activation_supported": False,
            "motion_usable": False,
        }
    )


def main() -> None:
    uvicorn.run(
        "stationary_world_arm_alignment.app:app",
        host=str(config["gui"]["host"]),
        port=int(config["gui"]["port"]),
        reload=False,
    )


if __name__ == "__main__":
    main()
