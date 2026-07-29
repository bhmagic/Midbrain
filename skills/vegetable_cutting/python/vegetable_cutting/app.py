from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from .alignment_gui import build_alignment_gui_command
from .artifacts import MonitorArtifacts
from .config import Settings, WORKSPACE_ROOT, load_skill_config
from .models import Phase, RunParameters, SkillState
from .skill import VegetableCuttingSkill


class StartRequest(BaseModel):
    slice_spacing_mm: float = Field(default=15.0, ge=1.0, le=200.0)
    blade_yaw_deg: float = Field(default=0.0, ge=-180.0, le=180.0)
    maximum_cut_count: int = Field(default=40, ge=1, le=100)


class WorkpieceConfirmation(BaseModel):
    operator_outside_workspace: bool = False


class ToolConfirmation(BaseModel):
    operator_confirms_knife_attached: bool = False


class AbortRequest(BaseModel):
    reason: str = "operator requested abort"


class ExecutionStartRequest(BaseModel):
    operator_takeover_confirmed: bool = False


class FirstCutDecisionRequest(BaseModel):
    decision: str


settings = Settings()
config = load_skill_config()
artifacts = MonitorArtifacts()
skill = VegetableCuttingSkill(settings=settings, config=config, artifacts=artifacts)
WEB_ROOT = Path(__file__).resolve().parent / "web"
GUI_PID_PATH = settings.run_root / "gui.pid.json"
GUI_STARTED_AT_US = time.time_ns() // 1000
provider_task: asyncio.Task[dict[str, Any]] | None = None
ALIGNMENT_GUI_URL = str(config["gui"]["alignment_url"]).rstrip("/")


def auto_bootstrap_providers_enabled() -> bool:
    return os.getenv("MIDBRAIN_GUI_AUTO_BOOTSTRAP_PROVIDERS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _consume_task(task: asyncio.Task[Any]) -> None:
    try:
        task.result()
    except (asyncio.CancelledError, Exception):
        pass


def gui_restart_block_reason(
    progress: dict[str, Any],
    integrated_state: dict[str, Any],
) -> str | None:
    trajectory = integrated_state.get("trajectory")
    trajectory_active = bool(
        trajectory
        and (
            not isinstance(trajectory, dict)
            or trajectory.get("active", True)
        )
    )
    if trajectory_active:
        return (
            "GUI restart is blocked while an Integrated trajectory is active. "
            "Request Float or wait for the one-shot motion to finish."
        )
    phase = str(progress.get("phase") or "").upper()
    if phase in {
        str(Phase.TRANSFER_TO_FIRST_CUT),
        str(Phase.CUTTING),
        str(Phase.SAFE_TERMINATING),
    }:
        return (
            f"GUI restart is blocked during {phase}. Request Float or wait "
            "for the physical sequence to become idle."
        )
    return None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global provider_task
    GUI_PID_PATH.write_text(
        json.dumps(
            {
                "gui": os.getpid(),
                "started_at_us": time.time_ns() // 1000,
                "owner": "vegetable_cutting.app",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    if auto_bootstrap_providers_enabled():
        provider_task = asyncio.create_task(
            skill.bootstrap_providers(),
            name="vegetable-cutting-provider-bootstrap",
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
        await skill.close()
        try:
            record = json.loads(GUI_PID_PATH.read_text(encoding="utf-8"))
            if int(record.get("gui", -1)) == os.getpid():
                GUI_PID_PATH.unlink(missing_ok=True)
        except Exception:
            pass


app = FastAPI(title="Supervised Vegetable Cutting Skill", lifespan=lifespan)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB_ROOT / "index.html")


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "service": "vegetable-cutting-planner",
            "process_id": os.getpid(),
            "started_at_us": GUI_STARTED_AT_US,
            "passive_gui_startup": not auto_bootstrap_providers_enabled(),
            "motion_submission_capability_available": bool(
                config["execution"]["enabled"]
            ),
        }
    )


@app.get("/assets/{name}")
async def asset(name: str) -> FileResponse:
    if name not in {"app.js", "style.css"}:
        raise HTTPException(404)
    return FileResponse(WEB_ROOT / name)


@app.get("/api/status")
async def status() -> JSONResponse:
    progress = await skill.progress.snapshot()
    session_alignment = progress.get("alignment") or {}
    live_alignment = await skill.alignment_status()
    if (
        isinstance(session_alignment, dict)
        and session_alignment.get("alignment_id")
        == live_alignment.get("alignment_id")
    ):
        progress["alignment"] = {
            **session_alignment,
            **live_alignment,
        }
    else:
        progress["alignment"] = live_alignment
    latest_plan = skill.store.latest()
    if (
        not progress.get("plan_id")
        or not isinstance(latest_plan, dict)
        or latest_plan.get("plan_id") != progress.get("plan_id")
    ):
        latest_plan = None
    return JSONResponse(
        {
            "progress": progress,
            "latest_plan": latest_plan,
            "motion_boundary": {
                "motion_submission_capability_available": bool(
                    config["execution"]["enabled"]
                ),
                "motion_submission_enabled_for_session": bool(
                    progress["motion_submission_enabled"]
                ),
                "operator_takeover_required": True,
                "integrated_access": "OPERATOR_SUPERVISED_CONTROL",
            },
        }
    )


@app.post("/api/gui/restart")
async def restart_gui() -> JSONResponse:
    return JSONResponse(
        {
            "accepted": True,
            "reload_only": True,
            "process_id": os.getpid(),
            "started_at_us": GUI_STARTED_AT_US,
            "message": (
                "Cutting GUI reload accepted. The live Skill session, fixed "
                "camera transform, local-VIO stop state, Integrated, and "
                "other providers are preserved."
            ),
        }
    )


@app.get("/api/readiness")
async def readiness() -> JSONResponse:
    return JSONResponse(await skill.readiness_snapshot())


@app.post("/api/alignment/gui")
async def start_alignment_gui() -> JSONResponse:
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(f"{ALIGNMENT_GUI_URL}/health")
            if response.is_success and response.json().get("status") == "ok":
                return JSONResponse(
                    {
                        "status": "ready",
                        "reused": True,
                        "url": ALIGNMENT_GUI_URL,
                        "motion_submitted": False,
                    }
                )
    except (httpx.HTTPError, ValueError):
        pass

    process: asyncio.subprocess.Process | None = None
    try:
        command = build_alignment_gui_command(WORKSPACE_ROOT)
        creationflags = (
            subprocess.CREATE_NO_WINDOW
            if os.name == "nt"
            else 0
        )
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(WORKSPACE_ROOT),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=creationflags,
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=60.0,
        )
    except asyncio.TimeoutError as error:
        if process is not None and process.returncode is None:
            process.kill()
            await process.communicate()
        raise HTTPException(
            503,
            detail="alignment GUI launcher timed out",
        ) from error
    except (OSError, RuntimeError) as error:
        raise HTTPException(
            503,
            detail=f"alignment GUI could not be started: {error}",
        ) from error
    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        if not detail:
            detail = stdout.decode("utf-8", errors="replace").strip()
        raise HTTPException(
            503,
            detail=f"alignment GUI launcher failed: {detail or process.returncode}",
        )
    return JSONResponse(
        {
            "status": "ready",
            "reused": False,
            "url": ALIGNMENT_GUI_URL,
            "motion_submitted": False,
        }
    )


@app.post("/api/providers/bootstrap")
async def bootstrap_providers() -> JSONResponse:
    global provider_task
    if provider_task is not None and not provider_task.done():
        return JSONResponse({"accepted": False, "reason": "Provider bootstrap is active"})
    provider_task = asyncio.create_task(
        skill.bootstrap_providers(),
        name="vegetable-cutting-provider-bootstrap",
    )
    provider_task.add_done_callback(_consume_task)
    return JSONResponse({"accepted": True})


@app.post("/api/camera/capture")
async def capture_camera() -> JSONResponse:
    try:
        return JSONResponse(await skill.capture_visual_snapshot())
    except RuntimeError as error:
        raise HTTPException(409, detail=str(error)) from error


@app.post("/api/session/start")
async def start_session(request: StartRequest) -> JSONResponse:
    try:
        result = await skill.start_session(
            RunParameters(
                slice_spacing_mm=request.slice_spacing_mm,
                blade_yaw_deg=request.blade_yaw_deg,
                maximum_cut_count=request.maximum_cut_count,
            )
        )
    except (RuntimeError, ValueError) as error:
        raise HTTPException(409, detail=str(error)) from error
    return JSONResponse(result)


@app.post("/api/session/reset-failed")
async def reset_failed_session() -> JSONResponse:
    try:
        return JSONResponse(await skill.reset_failed_session())
    except RuntimeError as error:
        raise HTTPException(409, detail=str(error)) from error


@app.post("/api/session/confirm-tool")
async def confirm_tool(request: ToolConfirmation) -> JSONResponse:
    try:
        return JSONResponse(
            await skill.confirm_tool_loaded(
                operator_confirms_knife_attached=(
                    request.operator_confirms_knife_attached
                )
            )
        )
    except (RuntimeError, ValueError) as error:
        raise HTTPException(409, detail=str(error)) from error


@app.post("/api/session/confirm-workpiece")
async def confirm_workpiece(request: WorkpieceConfirmation) -> JSONResponse:
    try:
        return JSONResponse(
            await skill.confirm_workpiece_loaded(
                operator_outside_workspace=request.operator_outside_workspace
            )
        )
    except (RuntimeError, ValueError) as error:
        raise HTTPException(409, detail=str(error)) from error


@app.post("/api/session/plan")
async def plan() -> JSONResponse:
    try:
        return JSONResponse(await skill.perceive_and_plan())
    except (RuntimeError, ValueError) as error:
        current = await skill.progress.snapshot()
        evidence = await skill.capture_failure_evidence(
            error,
            context="PLANNING",
            progress=current,
        )
        await skill.progress.update(
            state=SkillState.FAILED,
            phase=Phase.FAILED,
            message=f"Planning failed: {error}",
            execution={
                **(current.get("execution") or {}),
                "state": "PLANNING_FAILED",
                "failure_evidence": evidence,
            },
            error=str(error),
        )
        raise HTTPException(409, detail=str(error)) from error


@app.post("/api/session/execute")
async def execute(request: ExecutionStartRequest) -> JSONResponse:
    try:
        return JSONResponse(
            await skill.begin_execution(
                operator_takeover_confirmed=(
                    request.operator_takeover_confirmed
                )
            )
        )
    except (RuntimeError, ValueError) as error:
        current = await skill.progress.snapshot()
        evidence = await skill.capture_failure_evidence(
            error,
            context="EXECUTION_START_VALIDATION",
            progress=current,
        )
        await skill.progress.update(
            execution={
                **(current.get("execution") or {}),
                "state": "EXECUTION_START_REJECTED",
                "failure_evidence": evidence,
            },
            error=str(error),
        )
        raise HTTPException(409, detail=str(error)) from error


@app.post("/api/session/first-cut-decision")
async def first_cut_decision(
    request: FirstCutDecisionRequest,
) -> JSONResponse:
    try:
        return JSONResponse(
            await skill.first_cut_decision(request.decision)
        )
    except (RuntimeError, ValueError) as error:
        raise HTTPException(409, detail=str(error)) from error


@app.post("/api/session/tool-removed-safe-terminate")
async def tool_removed_safe_terminate() -> JSONResponse:
    try:
        return JSONResponse(
            await skill.confirm_tool_removed_and_safe_terminate()
        )
    except (RuntimeError, ValueError) as error:
        raise HTTPException(409, detail=str(error)) from error


@app.post("/api/session/abort")
async def abort(request: AbortRequest) -> JSONResponse:
    return JSONResponse(await skill.abort(request.reason))


@app.get("/api/image/{kind}")
async def image(kind: str) -> Response:
    if kind not in {"rgb", "depth", "overlay"}:
        raise HTTPException(404)
    payload = await artifacts.image(kind)
    if payload is None:
        raise HTTPException(404, detail=f"{kind} image is unavailable")
    media_type = "image/png" if kind == "depth" else "image/jpeg"
    return Response(payload, media_type=media_type, headers={"Cache-Control": "no-store"})


@app.get("/api/plan")
async def current_plan() -> JSONResponse:
    plan_value = await artifacts.plan_snapshot()
    if plan_value is None:
        raise HTTPException(404, detail="no plan has been generated")
    return JSONResponse(plan_value)


@app.get("/api/plans")
async def plans() -> JSONResponse:
    return JSONResponse({"plans": skill.store.list()})


def main() -> None:
    uvicorn.run(
        app,
        host=str(config["gui"]["host"]),
        port=int(config["gui"]["port"]),
        reload=False,
    )


if __name__ == "__main__":
    main()
