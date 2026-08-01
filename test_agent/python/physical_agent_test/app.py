from __future__ import annotations

import asyncio
import json
import math
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from agents import RunState, SessionSettings, SQLiteSession
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from .agent_driver import (
    AgentSessionAuthorization,
    PrototypeAgentDriver,
    relative_motion_within_authorization,
)
from .authorization import AuthorizationStore
from .basic_client import BasicControllerClient
from .basic_safe_home_adapter import BasicSafeHomeAdapter
from .config import Settings
from .depth_capture import DepthCapture
from .effector_front_adapter import EffectorFrontSkillAdapter
from .fabric_client import FabricClient
from .gemini_pointing_skill import (
    PointingIdentificationSkill,
    VisualSceneAnalysisSkill,
)
from .initialize_space_cognition_skill import InitializeSpaceCognitionSkill
from .integrated_client import IntegratedControllerClient
from .integrated_motion_adapter import IntegratedRelativeMotionAdapter
from .manager_client import ManagerClient
from .observation_motion import (
    attach_controller_preview,
    build_observation_motion_proposal,
    create_observation_motion_authorization,
)
from .phase4_policy import (
    OperationRegistry,
    Phase4Policy,
    install_operation_registry,
)
from .phase5_replay import Phase5ReplayCaptureService
from .rgb_capture import RgbCapture
from .rgbd_alignment import (
    RgbdAlignmentValidationSkill,
    RgbdEvidenceCapture,
)
from .reviewed_observation_execution import (
    ReviewedObservationExecutionAdapter,
)
from .skill_catalog import discover_agent_skills
from .spatial_registration_adapter import SpatialRegistrationSkillAdapter
from .spatial_frames import SpatialFrameResolver, rotation_matrix
from .stationary_calibration_adapter import StationaryCalibrationSkillAdapter
from .stationary_calibration_activation import (
    StationaryCalibrationActivationService,
)
from .tool_registration_adapter import ToolControlFrameSkillAdapter
from .world_point_cloud import WorldPointCloudAccumulator
from .vlm_router import build_default_vlm_router
from stationary_world_arm_alignment.camera import RgbdCapture
from stationary_world_arm_alignment.config import (
    Settings as StationaryAlignmentSettings,
)
from stationary_world_arm_alignment.skill import AlignmentSkill

settings = Settings()
phase4_policy = Phase4Policy.from_environment()
operation_registry = OperationRegistry()
install_operation_registry(operation_registry)
fabric = FabricClient(settings.fabric_url)
manager = ManagerClient(settings.manager_url)
integrated = IntegratedControllerClient(
    settings.integrated_controller_url,
    timeout_s=settings.integrated_preview_timeout_s,
)
spatial_frame_resolver = SpatialFrameResolver(
    fabric,
    arm_base_frame=settings.arm_base_frame,
)
basic = BasicControllerClient(
    settings.basic_controller_url,
    timeout_s=settings.basic_operation_timeout_s,
)
basic_safe_home_agent_adapter = BasicSafeHomeAdapter(basic)
authorization_store = AuthorizationStore(
    signing_secret=settings.authorization_signing_secret,
)
capture = RgbCapture(fabric, settings.screenshot_dir)
depth_capture = DepthCapture(fabric, settings.screenshot_dir)
agent_vlm_router = build_default_vlm_router(
    gemini_model=settings.gemini_model,
    attempt_timeout_s=phase4_policy.vlm_attempt_timeout_s,
)
pointing_skill = PointingIdentificationSkill(
    capture,
    settings.gemini_model,
    manager=manager,
    fallback_camera_provider_id=settings.head_camera_provider_id,
    vlm_router=agent_vlm_router,
)
visual_scene_skill = VisualSceneAnalysisSkill(
    capture,
    settings.gemini_model,
    manager=manager,
    fallback_camera_provider_id=settings.head_camera_provider_id,
    vlm_router=agent_vlm_router,
)
rgbd_evidence_capture = RgbdEvidenceCapture(
    fabric,
    settings.screenshot_dir,
    policy=phase4_policy,
)
rgbd_alignment_skill = RgbdAlignmentValidationSkill(
    rgbd_evidence_capture,
    agent_vlm_router,
    provider_id=settings.head_camera_provider_id,
    manager=manager,
    policy=phase4_policy,
)
spatial_registration_skill = SpatialRegistrationSkillAdapter(
    RgbdCapture(fabric, settings.head_camera_frame),
    fabric,
    manager=manager,
    fallback_camera_provider_id=settings.head_camera_provider_id,
    binding_mode=settings.phase5_spatial_binding_mode,
    generic_route_mode=settings.phase5_spatial_generic_route_mode,
)
effector_front_skill = EffectorFrontSkillAdapter(
    spatial_registration_skill,
    agent_vlm_router,
    evidence_dir=settings.screenshot_dir,
)
tool_registration_skill = ToolControlFrameSkillAdapter(
    spatial_registration_skill,
    agent_vlm_router,
    manager=manager,
    fallback_arm_provider_id=settings.arm_transform_provider_id,
    arm_base_frame=settings.arm_base_frame,
    arm_tool_frame=settings.arm_tool_frame,
    binding_mode=settings.phase5_spatial_binding_mode,
)
stationary_alignment_settings = StationaryAlignmentSettings()
stationary_calibration_activation = StationaryCalibrationActivationService(
    manager,
    review_auth_secret=settings.review_auth_secret,
    calibration_root=stationary_alignment_settings.calibration_root,
    review_root=stationary_alignment_settings.review_root,
)
stationary_calibration_agent_adapter = StationaryCalibrationSkillAdapter(
    AlignmentSkill,
    operation_hard_timeout_s=settings.stationary_calibration_timeout_s,
    activation_service=stationary_calibration_activation,
)
reviewed_observation_execution_skill = ReviewedObservationExecutionAdapter(
    authorization_store,
    integrated,
)
space_cognition_skill = InitializeSpaceCognitionSkill(
    manager,
    fabric,
    camera_provider_id=settings.head_camera_provider_id,
    vio_provider_id=settings.local_vio_provider_id,
    timeout_s=settings.space_cognition_timeout_s,
)
world_point_cloud = WorldPointCloudAccumulator(
    fabric,
    retention_s=settings.point_cloud_retention_s,
    sample_stride=settings.point_cloud_sample_stride,
    update_hz=settings.point_cloud_hz,
    max_points=settings.point_cloud_max_points,
)


async def _reinitialize_space_cognition(reason: str) -> dict[str, Any]:
    await world_point_cloud.begin_reinitialization()
    try:
        result = await space_cognition_skill.run(force_reset=True)
        reset_result = result.get("result") or {}
        session_epoch = str(reset_result.get("session_epoch") or "")
        world_frame = str(reset_result.get("world_frame") or "")
        if not session_epoch or not world_frame:
            raise RuntimeError(
                "forced reinitialization did not return a VIO session"
            )
        await world_point_cloud.switch_session(
            session_epoch=session_epoch,
            world_frame=world_frame,
        )
        result["requested_reason"] = reason
        result["point_cloud_resumed"] = (
            await world_point_cloud.wait_for_points(
                session_epoch=session_epoch,
                timeout_s=10.0,
            )
        )
        if result["point_cloud_resumed"]:
            result["message"] = (
                "Origin reinitialized. The previous map was cleared because "
                "it belonged to the old coordinate epoch, and new point "
                "capture has resumed."
            )
        else:
            result["message"] = (
                "Origin reinitialized and the new epoch is active, but point "
                "capture is still waiting for visual TRACKING and a fresh "
                "RGB-D transform."
            )
        return result
    except BaseException:
        await world_point_cloud.resume_follow_latest()
        raise


async def _verify_fixed_vio_rig(reason: str) -> dict[str, Any]:
    result = await space_cognition_skill.verify_tracking(
        fixed_rig_confirmed=True
    )
    result["requested_reason"] = reason
    return result


async def _capture_effector_visual_evidence(
    world_frame: str,
) -> dict[str, Any]:
    return await effector_front_skill.run(target_frame=world_frame)


integrated_motion_agent_adapter = IntegratedRelativeMotionAdapter(
    integrated,
    spatial_frame_resolver,
    vio_readiness_checker=_verify_fixed_vio_rig,
    visual_evidence_capture=_capture_effector_visual_evidence,
    require_visual_verification=False,
    attempt_visual_verification=True,
    require_upright_mount_confirmation=True,
    calibration_activation_continuation=(
        stationary_calibration_agent_adapter.latest_activation_continuation
    ),
)


discoverable_agent_skill_catalog = discover_agent_skills(
    settings.workspace_root,
)
all_discoverable_tool_names = {
    descriptor.tool_name
    for descriptor in discoverable_agent_skill_catalog
}
agent_session_database = (
    settings.workspace_root
    / "test_agent"
    / "run"
    / "agent_sessions.sqlite3"
)
agent_session_database.parent.mkdir(parents=True, exist_ok=True)
agent_runtime_session_epoch = uuid.uuid4().hex
driver = PrototypeAgentDriver(
    pointing_skill,
    settings.openai_model,
    tool_choice=settings.openai_agent_tool_choice,
    eligible_tool_names=set(settings.phase4_eligible_tools),
    visual_scene_skill=visual_scene_skill,
    rgbd_alignment_skill=rgbd_alignment_skill,
    spatial_registration_skill=spatial_registration_skill,
    effector_front_skill=effector_front_skill,
    tool_registration_skill=tool_registration_skill,
    stationary_calibration_skill=stationary_calibration_agent_adapter,
    manager=manager,
    provider_lifecycle_control=True,
    integrated_motion_skill=integrated_motion_agent_adapter,
    basic_safe_home_skill=basic_safe_home_agent_adapter,
    space_cognition_reinitializer=_reinitialize_space_cognition,
    session=SQLiteSession(
        (
            "midbrain-regular-agent-systemic-gui-v4-"
            f"{agent_runtime_session_epoch}"
        ),
        agent_session_database,
        session_settings=SessionSettings(limit=None),
    ),
    defer_loading=settings.agent_skill_defer_loading,
    adapter_timeout_s=phase4_policy.skill_adapter_timeout_s,
    stationary_calibration_timeout_s=(
        settings.stationary_calibration_timeout_s
    ),
    max_turns=settings.openai_agent_max_turns,
    session_history_item_limit=(
        settings.openai_agent_session_history_items
    ),
)
developer_driver = PrototypeAgentDriver(
    pointing_skill,
    settings.openai_model,
    tool_choice="auto",
    workspace_root=settings.workspace_root,
    eligible_tool_names=all_discoverable_tool_names,
    visual_scene_skill=visual_scene_skill,
    rgbd_alignment_skill=rgbd_alignment_skill,
    spatial_registration_skill=spatial_registration_skill,
    effector_front_skill=effector_front_skill,
    tool_registration_skill=tool_registration_skill,
    stationary_calibration_skill=stationary_calibration_agent_adapter,
    reviewed_observation_execution_skill=(
        reviewed_observation_execution_skill
    ),
    manager=manager,
    developer_mode=True,
    integrated_motion_skill=integrated_motion_agent_adapter,
    basic_safe_home_skill=basic_safe_home_agent_adapter,
    space_cognition_reinitializer=_reinitialize_space_cognition,
    session=SQLiteSession(
        (
            "midbrain-developer-agent-systemic-gui-v4-"
            f"{agent_runtime_session_epoch}"
        ),
        agent_session_database,
        session_settings=SessionSettings(limit=None),
    ),
    defer_loading=settings.agent_skill_defer_loading,
    adapter_timeout_s=phase4_policy.skill_adapter_timeout_s,
    stationary_calibration_timeout_s=(
        settings.stationary_calibration_timeout_s
    ),
    max_turns=settings.openai_agent_max_turns,
    session_history_item_limit=(
        settings.openai_agent_session_history_items
    ),
)
reviewed_observation_agent_driver = PrototypeAgentDriver(
    pointing_skill,
    settings.openai_model,
    tool_choice="required",
    eligible_tool_names={"execute_reviewed_observation_motion"},
    reviewed_observation_execution_skill=(
        reviewed_observation_execution_skill
    ),
    defer_loading=False,
    adapter_timeout_s=phase4_policy.skill_adapter_timeout_s,
    max_turns=settings.openai_agent_max_turns,
)
agent_skill_catalog = discover_agent_skills(
    settings.workspace_root,
    include_disabled=True,
)


@dataclass
class PendingAgentRun:
    state: RunState[Any]
    created_monotonic: float
    agent_model: str
    reasoning_effort: str
    vlm_model: str | None
    authorization: AgentSessionAuthorization = AgentSessionAuthorization()
    approval_decisions: dict[str, bool] = field(default_factory=dict)


agent_model_options = tuple(
    dict.fromkeys((settings.openai_model, *settings.openai_agent_models))
)
agent_reasoning_options = ("low", "medium", "high", "xhigh", "max")
vlm_model_options = tuple(
    dict.fromkeys(backend.model_id for backend in agent_vlm_router.backends)
)
pending_regular_runs: dict[str, PendingAgentRun] = {}
pending_regular_runs_lock = asyncio.Lock()
pending_developer_runs: dict[str, PendingAgentRun] = {}
pending_developer_runs_lock = asyncio.Lock()
replay_capture = Phase5ReplayCaptureService(fabric, settings.replay_bundle_dir)
auto_initialization_task: asyncio.Task[None] | None = None
auto_initialization_error: str | None = None
auto_initialization_state = "NOT_STARTED"
auto_initialization_result: dict[str, Any] | None = None


async def _auto_initialize() -> None:
    global auto_initialization_error, auto_initialization_result, auto_initialization_state
    try:
        auto_initialization_state = "SCHEDULED"
        await asyncio.sleep(1.0)
        auto_initialization_state = "RUNNING"
        auto_initialization_result = await space_cognition_skill.run(force_reset=False)
        auto_initialization_error = None
        auto_initialization_state = "SUCCEEDED"
    except asyncio.CancelledError:
        auto_initialization_state = "CANCELLED"
        raise
    except Exception as error:
        auto_initialization_error = str(error)
        auto_initialization_state = "FAILED"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global auto_initialization_task, auto_initialization_state
    await world_point_cloud.start()
    if settings.auto_initialize_space_cognition:
        auto_initialization_task = asyncio.create_task(
            _auto_initialize(),
            name="initialize-space-cognition",
        )
    else:
        auto_initialization_state = "DISABLED"
    yield
    if auto_initialization_task is not None:
        auto_initialization_task.cancel()
        try:
            await auto_initialization_task
        except asyncio.CancelledError:
            pass
        auto_initialization_task = None
    await world_point_cloud.stop()
    await basic.close()
    await integrated.close()
    await fabric.close()
    await manager.close()


app = FastAPI(
    title="Physical Agent Test Scaffold",
    version="0.4.2",
    lifespan=lifespan,
)

MAX_SESSION_AUTO_SPEED_M_S = 0.5


class PromptRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)
    agent_model: str = Field(
        default=settings.openai_model,
        min_length=1,
        max_length=100,
    )
    reasoning_effort: str = Field(
        default=settings.openai_agent_reasoning_effort,
        min_length=1,
        max_length=20,
    )
    vlm_model: str = Field(default="auto", min_length=1, max_length=100)
    auto_authorize_provider_activation: bool = False
    auto_authorize_relative_motion: bool = False
    max_auto_move_cm: float = Field(default=35.0, ge=0.1, le=100.0)
    max_auto_speed_m_s: float = Field(
        default=MAX_SESSION_AUTO_SPEED_M_S,
        gt=0.0,
        le=MAX_SESSION_AUTO_SPEED_M_S,
    )
    auto_authorize_stationary_calibration: bool = False
    auto_authorize_stationary_activation: bool = False


class RgbdAlignmentRequest(BaseModel):
    request: str = Field(
        default=(
            "Verify that this synchronized RGB and registered-depth bundle "
            "is visually and numerically aligned for observation use."
        ),
        min_length=1,
        max_length=2000,
    )


class Phase5ReplayCaptureRequest(BaseModel):
    bundle_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9._-]+$",
    )


class Phase5ReplayRouteComparisonRequest(BaseModel):
    pixel_yx: list[float] = Field(min_length=2, max_length=2)
    depth_policy: str = Field(
        default="ROBUST_MEDIAN",
        pattern=r"^(ROBUST_MEDIAN|CLOSEST_TO_CAMERA|NEAREST_VALID_PIXEL)$",
    )


class SpatialRegistrationRequest(BaseModel):
    pixel_yx: list[float] = Field(min_length=2, max_length=2)
    target_frame: str = Field(min_length=1, max_length=200)
    depth_policy: str = Field(
        default="ROBUST_MEDIAN",
        pattern=r"^(ROBUST_MEDIAN|CLOSEST_TO_CAMERA|NEAREST_VALID_PIXEL)$",
    )


class ToolRegistrationRequest(BaseModel):
    tool_description: str = Field(min_length=1, max_length=2000)
    control_frame_purpose: str = Field(min_length=1, max_length=2000)
    target_frame: str = Field(min_length=1, max_length=200)


class InitializationRequest(BaseModel):
    force_reset: bool = False


class AuthorizationCreateRequest(BaseModel):
    requester_type: str = Field(min_length=3, max_length=32)
    requester_id: str = Field(min_length=1, max_length=200)
    decision_type: str = Field(min_length=3, max_length=80)
    title: str = Field(min_length=3, max_length=200)
    summary: str = Field(min_length=3, max_length=2000)
    proposed_action: dict[str, Any]
    evidence: dict[str, Any] = Field(default_factory=dict)
    safety: dict[str, Any] = Field(default_factory=dict)
    expires_in_s: float = Field(default=120.0, ge=1.0, le=900.0)


class AuthorizationResolveRequest(BaseModel):
    resolution: str
    resolved_by: str = Field(default="local-operator", min_length=1, max_length=100)
    note: str | None = Field(default=None, max_length=1000)


class ObservationMotionProposalRequest(BaseModel):
    object_point_world_m: list[float] = Field(min_length=3, max_length=3)
    world_from_arm_base: list[float] = Field(min_length=16, max_length=16)
    view_mode: str
    standoff_m: float = Field(default=0.15, ge=0.05, le=0.50)
    source_evidence: dict[str, Any] = Field(default_factory=dict)
    preview_context: dict[str, Any]
    requester_id: str = Field(default="observe-pointed-object", min_length=1)


class ReviewedObservationAgentExecutionRequest(BaseModel):
    decision_id: str = Field(min_length=1, max_length=200)


class DeveloperApprovalDecision(BaseModel):
    approve: bool
    rejection_message: str | None = Field(default=None, max_length=500)
    approval_mode: str = Field(
        default="MANUAL",
        pattern=(
            r"^(MANUAL|AUTO_PROVIDER_ACTIVATION|"
            r"AUTO_BOUNDED_RELATIVE_MOTION|"
            r"AUTO_STATIONARY_CALIBRATION|"
            r"AUTO_STATIONARY_ACTIVATION)$"
        ),
    )
    max_auto_move_cm: float | None = Field(
        default=None,
        ge=0.1,
        le=100.0,
    )
    max_auto_speed_m_s: float | None = Field(
        default=None,
        gt=0.0,
        le=MAX_SESSION_AUTO_SPEED_M_S,
    )


def _approval_arguments(approval: dict[str, Any]) -> dict[str, Any]:
    request = approval.get("request")
    request = request if isinstance(request, dict) else {}
    raw_arguments = request.get("arguments", {})
    if isinstance(raw_arguments, dict):
        return raw_arguments
    if isinstance(raw_arguments, str):
        try:
            decoded = json.loads(raw_arguments)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _approval_fingerprint(approval: dict[str, Any]) -> str:
    """Identify an exact protected operation without SDK call identifiers."""
    return json.dumps(
        {
            "tool_name": str(approval.get("tool_name") or ""),
            "arguments": _approval_arguments(approval),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _record_approval_decisions(
    interruptions: list[Any],
    *,
    approved: bool,
    existing: dict[str, bool],
) -> dict[str, bool]:
    decisions = dict(existing)
    for interruption in interruptions:
        approval = PrototypeAgentDriver._approval_description(interruption)
        decisions[_approval_fingerprint(approval)] = bool(approved)
    return decisions


def _repeated_approval_response(
    *,
    run_id: str,
    approvals: list[dict[str, Any]],
    decisions: dict[str, bool],
) -> dict[str, Any] | None:
    repeated = [
        (approval, decisions[_approval_fingerprint(approval)])
        for approval in approvals
        if _approval_fingerprint(approval) in decisions
    ]
    if not repeated:
        return None
    descriptions = ", ".join(
        f"{approval.get('title') or approval.get('tool_name')} "
        f"({'previously approved' if approved else 'previously rejected'})"
        for approval, approved in repeated
    )
    return {
        "status": "completed",
        "run_id": run_id,
        "answer": (
            "Midbrain stopped a repeated approval loop. The agent requested "
            "the identical protected operation again after the current run "
            f"had already resolved it: {descriptions}. The duplicate "
            "operation was not authorized or executed. Inspect the current "
            "Provider state and start a new prompt if another transition is "
            "actually required."
        ),
        "approvals": [],
        "approval_loop_prevented": True,
    }


def _validate_automatic_agent_approval(
    interruptions: list[Any],
    decision: DeveloperApprovalDecision,
) -> None:
    if not decision.approve or decision.approval_mode == "MANUAL":
        return
    approvals = [
        PrototypeAgentDriver._approval_description(interruption)
        for interruption in interruptions
    ]
    if decision.approval_mode == "AUTO_PROVIDER_ACTIVATION":
        eligible = all(
            approval.get("tool_name") == "set_provider_residency"
            and str(
                _approval_arguments(approval).get("action") or ""
            ).lower()
            in {"start", "hot", "warm"}
            for approval in approvals
        )
        if not eligible:
            raise HTTPException(
                status_code=409,
                detail=(
                    "The session Provider authorization permits only start, "
                    "HOT, and WARM transitions; stop and other protected "
                    "operations still require an explicit decision."
                ),
            )
        return
    if decision.approval_mode == "AUTO_BOUNDED_RELATIVE_MOTION":
        maximum_cm = decision.max_auto_move_cm
        maximum_speed_m_s = decision.max_auto_speed_m_s
        if maximum_cm is None or not math.isfinite(maximum_cm):
            raise HTTPException(
                status_code=422,
                detail="bounded motion authorization requires a finite cm limit",
            )
        if (
            maximum_speed_m_s is None
            or not math.isfinite(maximum_speed_m_s)
        ):
            raise HTTPException(
                status_code=422,
                detail=(
                    "bounded motion authorization requires a finite nominal "
                    "speed limit"
                ),
            )
        eligible = all(
            approval.get("tool_name")
            == "execute_integrated_motion_preview"
            and relative_motion_within_authorization(
                _approval_arguments(approval),
                max_auto_move_cm=maximum_cm,
                max_auto_speed_m_s=maximum_speed_m_s,
            )
            for approval in approvals
        )
        if not eligible:
            raise HTTPException(
                status_code=409,
                detail=(
                    "The requested operation is not an exact Integrated "
                    "relative-pose preview within the browser-authorized "
                    f"{maximum_cm:g} cm and {maximum_speed_m_s:g} m/s "
                    "nominal-speed limits and the fixed 45-degree "
                    "controlled-frame-yaw limit."
                ),
            )
        return
    if decision.approval_mode == "AUTO_STATIONARY_CALIBRATION":
        eligible = all(
            approval.get("tool_name") == "calibrate_stationary_workcell"
            for approval in approvals
        )
        if not eligible:
            raise HTTPException(
                status_code=409,
                detail=(
                    "The session calibration authorization permits only "
                    "calibrate_stationary_workcell; physical motion and other "
                    "protected operations still require their own decision."
                ),
            )
        return
    if decision.approval_mode == "AUTO_STATIONARY_ACTIVATION":
        eligible = all(
            approval.get("tool_name")
            == "review_and_activate_stationary_calibration"
            for approval in approvals
        )
        if not eligible:
            raise HTTPException(
                status_code=409,
                detail=(
                    "The session calibration-activation authorization "
                    "permits only exact stationary candidate review and "
                    "bounded activation; physical motion and other protected "
                    "operations still require their own decision."
                ),
            )
        return
    raise HTTPException(
        status_code=422,
        detail="unknown automatic approval mode",
    )


REGULAR_PAGE = (
    Path(__file__).resolve().parent / "web" / "regular_agent.html"
).read_text(encoding="utf-8")
RGBD_ALIGNMENT_PAGE = (
    Path(__file__).resolve().parent / "web" / "rgbd_alignment.html"
).read_text(encoding="utf-8")
INTEGRATED_RELATIVE_MOTION_PAGE = (
    Path(__file__).resolve().parent
    / "web"
    / "integrated_relative_motion.html"
).read_text(encoding="utf-8")
@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return REGULAR_PAGE


@app.get("/dev", response_class=HTMLResponse)
async def developer_index() -> str:
    return PAGE


@app.get(
    "/dev/skills/initialize-space-cognition",
    response_class=HTMLResponse,
)
async def space_cognition_developer_index() -> str:
    return PAGE


@app.get(
    "/dev/skills/verify-rgbd-alignment",
    response_class=HTMLResponse,
)
async def rgbd_alignment_developer_index() -> str:
    return RGBD_ALIGNMENT_PAGE


@app.get(
    "/dev/skills/integrated-relative-effector-motion",
    response_class=HTMLResponse,
)
async def integrated_relative_motion_developer_index() -> str:
    return INTEGRATED_RELATIVE_MOTION_PAGE


@app.get("/dev/spatial-axes", response_class=HTMLResponse)
async def spatial_axes_developer_index() -> str:
    return PAGE


@app.get("/health")
async def ui_health() -> dict[str, str]:
    return {"status": "ok", "service": "physical-agent-ui"}


async def _safe(call) -> Any:
    try:
        return await call()
    except Exception as error:
        return {"status": "error", "error": str(error)}


@app.get("/api/status")
async def status() -> dict[str, Any]:
    (
        manager_health,
        fabric_health,
        providers,
        initialization,
        vio_status,
        body_pose,
        motion_inhibit,
        cloud_status,
        system_overview,
    ) = await asyncio.gather(
        _safe(manager.health),
        _safe(fabric.health),
        _safe(manager.providers),
        _safe(lambda: fabric.latest_optional("skills.initialize_space_cognition.status")),
        _safe(lambda: fabric.latest_optional("localization.vio.status")),
        _safe(lambda: fabric.latest_optional("localization.body.pose")),
        _safe(manager.motion_inhibit_status),
        _safe(world_point_cloud.status),
        _safe(manager.ui_overview),
    )
    return {
        "manager": manager_health,
        "fabric": fabric_health,
        "providers": providers,
        "space_cognition": initialization,
        "vio": vio_status,
        "body_pose": body_pose,
        "motion_inhibit": motion_inhibit,
        "world_point_cloud": cloud_status,
        "system_catalog": system_overview,
        "auto_initialize_enabled": settings.auto_initialize_space_cognition,
        "auto_initialize_state": auto_initialization_state,
        "auto_initialize_result": auto_initialization_result,
        "auto_initialize_error": auto_initialization_error,
        "openai_model": settings.openai_model,
        "agent_model_options": list(agent_model_options),
        "agent_reasoning_effort": settings.openai_agent_reasoning_effort,
        "agent_reasoning_options": list(agent_reasoning_options),
        "openai_agent_tool_choice": settings.openai_agent_tool_choice,
        "agent_session_history_item_limit": (
            settings.openai_agent_session_history_items
        ),
        "stationary_calibration_timeout_s": (
            settings.stationary_calibration_timeout_s
        ),
        "gemini_model": settings.gemini_model,
        "vlm_model_options": ["auto", *vlm_model_options],
        "capability_binding": pointing_skill.last_binding,
        "rgbd_alignment_binding": rgbd_alignment_skill.last_binding,
        "rgbd_alignment_validation": rgbd_alignment_skill.last_result,
        "spatial_registration_binding": spatial_registration_skill.last_binding,
        "spatial_registration_result": spatial_registration_skill.last_result,
        "effector_front_result": effector_front_skill.last_result,
        "tool_registration_binding": tool_registration_skill.last_binding,
        "tool_registration_result": tool_registration_skill.last_result,
        "agent_skill_catalog": [
            descriptor.as_dict() for descriptor in agent_skill_catalog
        ],
        "authorization_ui": {
            "pending_count": len(authorization_store.list(status="PENDING")),
            "approval_executes_action": False,
            "ui_role": "OBSERVATION_AND_DECISION_ONLY",
        },
        "phase4_policy": phase4_policy.as_dict(),
        "phase5_policy": {
            "spatial_binding": settings.phase5_spatial_binding_mode,
            "spatial_generic_rgbd_route": (
                settings.phase5_spatial_generic_route_mode
            ),
        },
        "bounded_operations": operation_registry.snapshot(),
    }


@app.get("/api/skills/integrated-relative-effector-motion/status")
async def integrated_relative_motion_status() -> dict[str, Any]:
    return await integrated_motion_agent_adapter.observation()


@app.get("/api/spatial/axes")
async def spatial_axes() -> dict[str, Any]:
    vio_observation, transform_edges = await asyncio.gather(
        fabric.latest_optional("localization.vio.status"),
        fabric.transforms(),
    )
    vio_data = (
        vio_observation.get("data")
        if isinstance(vio_observation, dict)
        else None
    )
    if not isinstance(vio_data, dict):
        raise HTTPException(
            status_code=409,
            detail="Local VIO has not published a current spatial epoch",
        )
    convention_id = str(vio_data.get("convention_id") or "")
    if convention_id != "MIDBRAIN_X_FORWARD_Y_LEFT_Z_UP_V2":
        raise HTTPException(
            status_code=409,
            detail=(
                "The active VIO epoch uses a legacy or unknown convention; "
                "reset VIO before inspecting convention-V2 axes"
            ),
        )
    world_frame = str(vio_data.get("world_frame") or "")
    session_epoch = str(vio_data.get("session_epoch") or "")
    observed_at_us = int(
        vio_observation.get("observed_at_us")
        or time.time_ns() // 1000
    )
    if not world_frame or not session_epoch:
        raise HTTPException(
            status_code=409,
            detail="VIO frame/epoch identity is incomplete",
        )
    names = {
        world_frame,
        "body_base",
        settings.head_camera_frame,
        settings.arm_base_frame,
        settings.arm_tool_frame,
        *(f"link{index}" for index in range(1, 7)),
    }
    camera_level_frame = str(
        vio_data.get("camera_level_frame") or ""
    )
    if camera_level_frame:
        names.add(camera_level_frame)
    for edge in transform_edges:
        parent = str(edge.get("parent_frame") or "")
        child = str(edge.get("child_frame") or "")
        if parent:
            names.add(parent)
        if child:
            names.add(child)

    async def resolve_frame(frame_id: str) -> dict[str, Any]:
        frame_key = frame_id.lower()
        if frame_id == world_frame:
            frame_role = "WORLD"
        elif frame_id == settings.arm_base_frame:
            frame_role = "ARM_BASE"
        elif frame_id == settings.arm_tool_frame or any(
            token in frame_key for token in ("gripper", "tool", "effector")
        ):
            frame_role = "GRIPPER_TOOL"
        elif frame_id in {f"link{index}" for index in range(1, 7)} or (
            "joint" in frame_key and "frame" in frame_key
        ):
            frame_role = "ARM_JOINT"
        elif frame_id == camera_level_frame:
            frame_role = "CAMERA_LEVEL"
        elif "optical_frame" in frame_key or frame_key.startswith(
            "camera_optical/"
        ):
            frame_role = "CAMERA_OPTICAL"
        elif frame_key.startswith("observed_object/") or "object" in frame_key:
            frame_role = "OBJECT"
        elif frame_id == "body_base":
            frame_role = "BODY"
        elif frame_key.endswith("_imu_frame"):
            frame_role = "SENSOR"
        else:
            frame_role = "OTHER"
        convention = (
            "CAMERA_OPTICAL_X_RIGHT_Y_DOWN_Z_FORWARD_V1"
            if "optical_frame" in frame_id
            or frame_id.startswith("camera_optical/")
            else "HARDWARE_CALIBRATED_LOCAL_FRAME"
            if frame_id.endswith("_imu_frame")
            else "ROBOT_KINEMATIC_LOCAL_FRAME"
            if frame_role in {"ARM_JOINT", "GRIPPER_TOOL"}
            else "MIDBRAIN_X_FORWARD_Y_LEFT_Z_UP_V2"
            if frame_id
            in {
                world_frame,
                "body_base",
                settings.arm_base_frame,
                camera_level_frame,
            }
            else "UNDECLARED_FRAME_LOCAL_AXES"
        )
        label = (
            "world"
            if frame_id == world_frame
            else "camera-level"
            if frame_id == camera_level_frame
            else "camera-optical"
            if convention
            == "CAMERA_OPTICAL_X_RIGHT_Y_DOWN_Z_FORWARD_V1"
            else "arm-base"
            if frame_id == settings.arm_base_frame
            else "gripper / tool"
            if frame_role == "GRIPPER_TOOL"
            else frame_id
            if frame_role == "ARM_JOINT"
            else "body"
            if frame_id == "body_base"
            else frame_id.rsplit("/", 1)[-1][:18]
        )
        if frame_id == world_frame:
            return {
                "frame_id": frame_id,
                "short_label": label,
                "frame_role": frame_role,
                "default_visible": True,
                "axis_length_m": 0.65,
                "convention_id": convention,
                "available": True,
                "translation_m": [0.0, 0.0, 0.0],
                "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
                "at_us": observed_at_us,
                "path": [],
            }
        try:
            transform = await fabric.transform(
                from_frame=frame_id,
                to_frame=world_frame,
                at_us=observed_at_us,
                max_extrapolation_us=500_000,
                session_epoch=session_epoch,
            )
            return {
                "frame_id": frame_id,
                "short_label": label,
                "frame_role": frame_role,
                "default_visible": frame_role
                in {
                    "ARM_BASE",
                    "GRIPPER_TOOL",
                    "CAMERA_OPTICAL",
                    "CAMERA_LEVEL",
                },
                "axis_length_m": (
                    0.32
                    if frame_role == "ARM_BASE"
                    else 0.24
                    if frame_role == "GRIPPER_TOOL"
                    else 0.16
                    if frame_role == "ARM_JOINT"
                    else 0.22
                ),
                "convention_id": convention,
                "available": True,
                "translation_m": transform["translation_m"],
                "rotation_xyzw": transform["rotation_xyzw"],
                "at_us": transform["at_us"],
                "path": transform.get("path") or [],
            }
        except Exception as error:
            return {
                "frame_id": frame_id,
                "short_label": label,
                "frame_role": frame_role,
                "default_visible": False,
                "axis_length_m": 0.16,
                "convention_id": convention,
                "available": False,
                "error": str(error),
            }

    frames = await asyncio.gather(
        *(resolve_frame(frame_id) for frame_id in sorted(names))
    )
    arm_frame = next(
        (
            frame
            for frame in frames
            if frame["frame_id"] == settings.arm_base_frame
        ),
        None,
    )
    world_semantics = {
        "FRONT": [1.0, 0.0, 0.0],
        "BACK": [-1.0, 0.0, 0.0],
        "LEFT": [0.0, 1.0, 0.0],
        "RIGHT": [0.0, -1.0, 0.0],
        "UP": [0.0, 0.0, 1.0],
        "DOWN": [0.0, 0.0, -1.0],
    }
    semantic_resolution: dict[str, Any] = {}
    if isinstance(arm_frame, dict) and arm_frame.get("available"):
        world_from_arm = rotation_matrix(arm_frame["rotation_xyzw"])
        for direction, vector_world in world_semantics.items():
            vector_arm = [
                sum(
                    world_from_arm[row][column] * vector_world[row]
                    for row in range(3)
                )
                for column in range(3)
            ]
            semantic_resolution[direction] = {
                "vector_world": vector_world,
                "vector_arm_base": vector_arm,
                "status": "RESOLVED",
            }
    else:
        semantic_resolution = {
            direction: {
                "vector_world": vector_world,
                "vector_arm_base": None,
                "status": "ARM_ALIGNMENT_UNAVAILABLE",
            }
            for direction, vector_world in world_semantics.items()
        }
    return {
        "schema": "physical_agent.spatial_axis_snapshot",
        "schema_version": 2,
        "convention_id": convention_id,
        "camera_optical_convention_id": (
            "CAMERA_OPTICAL_X_RIGHT_Y_DOWN_Z_FORWARD_V1"
        ),
        "observed_at_us": observed_at_us,
        "session_epoch": session_epoch,
        "world": {
            "frame_id": world_frame,
            "tracking_state": vio_data.get("tracking_state"),
            "positive_x": "forward",
            "positive_y": "left",
            "positive_z": "opposite gravity",
            "gravity_direction": [0.0, 0.0, -1.0],
        },
        "frames": list(frames),
        "edges": transform_edges,
        "semantic_resolution": semantic_resolution,
        "language_policy": {
            "ordinary_3d_frame": "WORLD",
            "plain_up": "OPPOSITE_GRAVITY",
            "plain_front": "WORLD_POSITIVE_X",
            "plain_left": "WORLD_POSITIVE_Y",
            "optical_axes_require_explicit_names": True,
            "image_language_requires_explicit_2d_request": True,
        },
        "screen_space": {
            "frame_id": "image_screen",
            "short_label": "2D screen space",
            "frame_role": "SCREEN_2D",
            "available": True,
            "default_visible": False,
            "positive_x": "right",
            "positive_y": "down",
            "origin": "top-left image pixel",
            "applies_only_when_explicit": True,
        },
    }


@app.get("/api/phase4/policy")
async def phase4_policy_status() -> dict[str, Any]:
    return {
        "policy": phase4_policy.as_dict(),
        "phase5_policy": {
            "spatial_binding": settings.phase5_spatial_binding_mode,
            "spatial_generic_rgbd_route": (
                settings.phase5_spatial_generic_route_mode
            ),
        },
        "operations": operation_registry.snapshot(),
    }


@app.get("/api/phase5/replay/bundles")
async def phase5_replay_bundles() -> dict[str, Any]:
    return {
        "hardware_access_allowed": False,
        "bundles": await asyncio.to_thread(replay_capture.list_bundles),
    }


@app.get("/api/phase5/replay/{bundle_id}/provenance")
async def phase5_replay_provenance(bundle_id: str) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(
            replay_capture.bundle_provenance,
            bundle_id,
        )
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.post("/api/phase5/replay/capture")
async def phase5_replay_capture(
    request: Phase5ReplayCaptureRequest,
) -> dict[str, Any]:
    try:
        return await operation_registry.run(
            "phase5_replay_capture",
            replay_capture.capture_current(
                bundle_id=request.bundle_id,
                additional_records={
                    "phase4_policy": phase4_policy.as_dict(),
                    "phase5_policy": {
                        "spatial_binding": (
                            settings.phase5_spatial_binding_mode
                        ),
                        "spatial_generic_rgbd_route": (
                            settings.phase5_spatial_generic_route_mode
                        ),
                    },
                    "bounded_operations_before_capture": (
                        operation_registry.snapshot()
                    ),
                    "capability_binding": rgbd_alignment_skill.last_binding,
                    "rgbd_alignment_validation": (
                        rgbd_alignment_skill.last_result
                    ),
                    "authorizations": authorization_store.list(),
                },
            ),
            hard_timeout_s=phase4_policy.operation_hard_timeout_s,
            idle_timeout_s=phase4_policy.operation_idle_timeout_s,
        )
    except FileExistsError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.post("/api/phase5/replay/{bundle_id}/validate")
async def phase5_replay_validate(bundle_id: str) -> dict[str, Any]:
    try:
        return await operation_registry.run(
            "phase5_replay_validate",
            replay_capture.validate_bundle(bundle_id),
            hard_timeout_s=phase4_policy.operation_hard_timeout_s,
            idle_timeout_s=phase4_policy.operation_idle_timeout_s,
        )
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.post("/api/phase5/replay/{bundle_id}/compare-rgbd-routes")
async def phase5_replay_compare_rgbd_routes(
    bundle_id: str,
    request: Phase5ReplayRouteComparisonRequest,
) -> dict[str, Any]:
    try:
        return await operation_registry.run(
            "phase5_replay_compare_rgbd_routes",
            replay_capture.compare_rgbd_routes(
                bundle_id,
                pixel_yx=(float(request.pixel_yx[0]), float(request.pixel_yx[1])),
                depth_policy=request.depth_policy,
            ),
            hard_timeout_s=phase4_policy.operation_hard_timeout_s,
            idle_timeout_s=phase4_policy.operation_idle_timeout_s,
        )
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (RuntimeError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@app.post("/api/phase5/replay/{bundle_id}/scenario/{scenario_name}")
async def phase5_replay_scenario(
    bundle_id: str,
    scenario_name: str,
) -> dict[str, Any]:
    try:
        return await operation_registry.run(
            "phase5_replay_scenario",
            replay_capture.run_scenario(bundle_id, scenario_name),
            hard_timeout_s=phase4_policy.operation_hard_timeout_s,
            idle_timeout_s=phase4_policy.operation_idle_timeout_s,
        )
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@app.post("/api/phase5/spatial/register")
async def phase5_spatial_register(
    request: SpatialRegistrationRequest,
) -> dict[str, Any]:
    try:
        return await operation_registry.run(
            "phase5_spatial_registration",
            spatial_registration_skill.run(
                pixel_yx=request.pixel_yx,
                target_frame=request.target_frame,
                depth_policy=request.depth_policy,
            ),
            hard_timeout_s=phase4_policy.operation_hard_timeout_s,
            idle_timeout_s=phase4_policy.operation_idle_timeout_s,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.post("/api/phase5/tool/register-candidate")
async def phase5_tool_register_candidate(
    request: ToolRegistrationRequest,
) -> dict[str, Any]:
    try:
        return await operation_registry.run(
            "phase5_tool_registration_candidate",
            tool_registration_skill.run(
                tool_description=request.tool_description,
                control_frame_purpose=request.control_frame_purpose,
                target_frame=request.target_frame,
            ),
            hard_timeout_s=phase4_policy.operation_hard_timeout_s,
            idle_timeout_s=phase4_policy.operation_idle_timeout_s,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.get("/api/skills")
async def skills(include_disabled: bool = False) -> dict[str, Any]:
    descriptors = discover_agent_skills(
        settings.workspace_root,
        include_disabled=include_disabled,
    )
    return {
        "selection": "OPENAI_AGENTS_SDK_TOOL_DESCRIPTION",
        "provider_binding": "MANAGER_ADVISORY",
        "skills": [descriptor.as_dict() for descriptor in descriptors],
    }


@app.get("/api/authorizations")
async def authorizations(status: str | None = None) -> dict[str, Any]:
    return {
        "approval_executes_action": False,
        "authorizations": authorization_store.list(status=status),
    }


@app.post("/api/authorizations")
async def create_authorization(
    request: AuthorizationCreateRequest,
) -> dict[str, Any]:
    return authorization_store.create(**request.model_dump())


@app.post("/api/authorizations/{decision_id}/resolve")
async def resolve_authorization(
    decision_id: str,
    request: AuthorizationResolveRequest,
) -> dict[str, Any]:
    try:
        return authorization_store.resolve(
            decision_id,
            resolution=request.resolution,
            resolved_by=request.resolved_by,
            note=request.note,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="authorization not found") from error
    except (ValueError, RuntimeError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/authorizations/{decision_id}/execution-assertion")
async def issue_authorization_assertion(
    decision_id: str,
) -> dict[str, Any]:
    try:
        return authorization_store.issue_execution_assertion(decision_id)
    except KeyError as error:
        raise HTTPException(
            status_code=404,
            detail="authorization not found",
        ) from error
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/observation-motion/execute/{decision_id}")
async def execute_observation_motion(
    decision_id: str,
) -> dict[str, Any]:
    """Execute only after a separate approval and one-time assertion issue."""

    try:
        return await reviewed_observation_execution_skill.run(
            decision_id=decision_id
        )
    except KeyError as error:
        raise HTTPException(
            status_code=404,
            detail="authorization not found",
        ) from error
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except httpx.HTTPStatusError as error:
        detail = error.response.text
        raise HTTPException(
            status_code=error.response.status_code,
            detail=detail,
        ) from error
    except httpx.HTTPError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.post("/api/agent/reviewed-observation/execute")
async def execute_reviewed_observation_through_agent(
    request: ReviewedObservationAgentExecutionRequest,
) -> dict[str, Any]:
    try:
        answer = await operation_registry.run(
            "agent_reviewed_observation_execution",
            reviewed_observation_agent_driver.run(
                "Execute the already reviewed and operator-approved "
                f"observation decision ID {request.decision_id}. "
                "Use the eligible finite skill and report its exact result."
            ),
            hard_timeout_s=settings.phase4_agent_run_timeout_s,
            idle_timeout_s=phase4_policy.operation_idle_timeout_s,
        )
        return {
            "decision_id": request.decision_id,
            "agent_sdk": True,
            "answer": answer,
        }
    except KeyError as error:
        raise HTTPException(
            status_code=404,
            detail="authorization not found",
        ) from error
    except (ValueError, RuntimeError, httpx.HTTPError) as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.post("/api/observation-motion/propose")
async def propose_observation_motion(
    request: ObservationMotionProposalRequest,
) -> dict[str, Any]:
    try:
        proposal = build_observation_motion_proposal(
            object_point_world_m=request.object_point_world_m,
            world_from_arm_base=request.world_from_arm_base,
            view_mode=request.view_mode,
            standoff_m=request.standoff_m,
            source_evidence=request.source_evidence,
            preview_context=request.preview_context,
        )
        preview = await integrated.preview_transit_path(
            proposal["controller_plan_request"]
        )
        proposal = attach_controller_preview(proposal, preview)
        if not proposal["controller_preview_valid"]:
            return {
                "proposal": proposal,
                "authorization": None,
                "physical_motion_authorized": False,
                "approval_executes_action": False,
            }
        authorization = create_observation_motion_authorization(
            authorization_store,
            proposal,
            requester_id=request.requester_id,
        )
        return {
            "proposal": proposal,
            "authorization": authorization,
            "physical_motion_authorized": False,
            "approval_executes_action": False,
        }
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except (httpx.HTTPError, RuntimeError) as error:
        raise HTTPException(
            status_code=503,
            detail=f"Integrated nonphysical path preview unavailable: {error}",
        ) from error


@app.post("/api/space-cognition/initialize")
async def initialize_space_cognition(request: InitializationRequest) -> dict[str, Any]:
    try:
        if request.force_reset:
            return await _reinitialize_space_cognition(
                "Operator requested reinitialization from the Agent GUI"
            )
        result = await space_cognition_skill.run(force_reset=False)
        if result.get("status") == "already_initialized":
            result["message"] = (
                "Space cognition was already initialized automatically at GUI startup. "
                "Use Force reinitialize only when a new local origin is required."
            )
        return result
    except httpx.HTTPStatusError as error:
        if request.force_reset:
            await world_point_cloud.resume_follow_latest()
        detail = error.response.text or str(error)
        raise HTTPException(status_code=502, detail=detail) from error
    except Exception as error:
        if request.force_reset:
            await world_point_cloud.resume_follow_latest()
        raise HTTPException(status_code=500, detail=str(error)) from error


@app.get("/api/world-point-cloud")
async def world_point_cloud_snapshot() -> Response:
    try:
        payload = await world_point_cloud.snapshot_binary()
        return Response(
            content=payload,
            media_type="application/octet-stream",
            headers={"Cache-Control": "no-store"},
        )
    except Exception as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.post("/api/world-point-cloud/clear")
async def clear_world_point_cloud() -> dict[str, str]:
    await world_point_cloud.clear()
    return {"status": "cleared"}


def _model_selection(
    request: PromptRequest,
) -> tuple[str, str, str | None]:
    agent_model = request.agent_model.strip()
    reasoning_effort = request.reasoning_effort.strip().lower()
    requested_vlm_model = request.vlm_model.strip()
    if agent_model not in agent_model_options:
        raise HTTPException(
            status_code=422,
            detail=f"unsupported Agent model: {agent_model}",
        )
    if reasoning_effort not in agent_reasoning_options:
        raise HTTPException(
            status_code=422,
            detail=f"unsupported reasoning effort: {reasoning_effort}",
        )
    vlm_model = None if requested_vlm_model == "auto" else requested_vlm_model
    if vlm_model is not None and vlm_model not in vlm_model_options:
        raise HTTPException(
            status_code=422,
            detail=f"unavailable VLM model: {vlm_model}",
        )
    return agent_model, reasoning_effort, vlm_model


def _session_authorization(
    request: PromptRequest,
) -> AgentSessionAuthorization:
    return AgentSessionAuthorization(
        auto_authorize_provider_activation=(
            request.auto_authorize_provider_activation
        ),
        auto_authorize_relative_motion=request.auto_authorize_relative_motion,
        max_auto_move_cm=request.max_auto_move_cm,
        max_auto_speed_m_s=request.max_auto_speed_m_s,
        auto_authorize_stationary_calibration=(
            request.auto_authorize_stationary_calibration
        ),
        auto_authorize_stationary_activation=(
            request.auto_authorize_stationary_activation
        ),
    )


@app.post("/api/run")
async def run_prompt(request: PromptRequest) -> dict[str, Any]:
    run_id = str(uuid.uuid4())
    agent_model, reasoning_effort, vlm_model = _model_selection(request)
    authorization = _session_authorization(request)
    try:
        return await _regular_agent_step(
            request.prompt,
            run_id,
            agent_model=agent_model,
            reasoning_effort=reasoning_effort,
            vlm_model=vlm_model,
            authorization=authorization,
        )
    except httpx.HTTPStatusError as error:
        detail = error.response.text or str(error)
        raise HTTPException(status_code=502, detail=detail) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


async def _regular_agent_step(
    input_value: str | RunState[Any],
    run_id: str,
    *,
    agent_model: str,
    reasoning_effort: str,
    vlm_model: str | None,
    authorization: AgentSessionAuthorization,
    approval_decisions: dict[str, bool] | None = None,
) -> dict[str, Any]:
    result = await operation_registry.run(
        f"openai_regular_agent_run:{run_id}",
        driver.run_interactive(
            input_value,
            model_override=agent_model,
            reasoning_effort=reasoning_effort,
            vlm_model_override=vlm_model,
            authorization=authorization,
        ),
        hard_timeout_s=min(
            settings.phase4_agent_run_timeout_s,
            phase4_policy.operation_hard_timeout_s,
        ),
        idle_timeout_s=phase4_policy.operation_idle_timeout_s,
    )
    if result.state is None:
        return {
            "status": "completed",
            "run_id": run_id,
            "answer": result.answer,
            "approvals": [],
        }
    decisions = dict(approval_decisions or {})
    repeated = _repeated_approval_response(
        run_id=run_id,
        approvals=result.approvals,
        decisions=decisions,
    )
    if repeated is not None:
        return repeated
    async with pending_regular_runs_lock:
        pending_regular_runs[run_id] = PendingAgentRun(
            state=result.state,
            created_monotonic=time.monotonic(),
            agent_model=agent_model,
            reasoning_effort=reasoning_effort,
            vlm_model=vlm_model,
            authorization=authorization,
            approval_decisions=decisions,
        )
    return {
        "status": "approval_required",
        "run_id": run_id,
        "answer": None,
        "approvals": result.approvals,
    }


@app.post("/api/runs/{run_id}/decision")
async def decide_regular_run(
    run_id: str,
    decision: DeveloperApprovalDecision,
) -> dict[str, Any]:
    async with pending_regular_runs_lock:
        entry = pending_regular_runs.pop(run_id, None)
        expired = [
            pending_id
            for pending_id, pending in pending_regular_runs.items()
            if time.monotonic() - pending.created_monotonic > 600.0
        ]
        for pending_id in expired:
            pending_regular_runs.pop(pending_id, None)
    if entry is None:
        raise HTTPException(
            status_code=404,
            detail="pending regular agent run was not found or expired",
        )
    if time.monotonic() - entry.created_monotonic > 600.0:
        raise HTTPException(
            status_code=410,
            detail="pending regular agent approval expired",
        )
    interruptions = entry.state.get_interruptions()
    if not interruptions:
        raise HTTPException(
            status_code=409,
            detail="regular agent run has no pending approvals",
        )
    try:
        _validate_automatic_agent_approval(interruptions, decision)
    except HTTPException:
        async with pending_regular_runs_lock:
            pending_regular_runs[run_id] = entry
        raise
    approval_decisions = _record_approval_decisions(
        interruptions,
        approved=decision.approve,
        existing=entry.approval_decisions,
    )
    for interruption in interruptions:
        if decision.approve:
            entry.state.approve(interruption)
        else:
            entry.state.reject(
                interruption,
                rejection_message=decision.rejection_message,
            )
    try:
        return await _regular_agent_step(
            entry.state,
            run_id,
            agent_model=entry.agent_model,
            reasoning_effort=entry.reasoning_effort,
            vlm_model=entry.vlm_model,
            authorization=entry.authorization,
            approval_decisions=approval_decisions,
        )
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


async def _developer_agent_step(
    input_value: str | RunState[Any],
    run_id: str,
    *,
    agent_model: str,
    reasoning_effort: str,
    vlm_model: str | None,
    authorization: AgentSessionAuthorization,
    approval_decisions: dict[str, bool] | None = None,
) -> dict[str, Any]:
    result = await operation_registry.run(
        f"openai_developer_agent_run:{run_id}",
        developer_driver.run_interactive(
            input_value,
            model_override=agent_model,
            reasoning_effort=reasoning_effort,
            vlm_model_override=vlm_model,
            authorization=authorization,
        ),
        hard_timeout_s=min(
            settings.phase4_agent_run_timeout_s,
            phase4_policy.operation_hard_timeout_s,
        ),
        idle_timeout_s=phase4_policy.operation_idle_timeout_s,
    )
    if result.state is None:
        return {
            "status": "completed",
            "run_id": run_id,
            "answer": result.answer,
            "approvals": [],
        }
    decisions = dict(approval_decisions or {})
    repeated = _repeated_approval_response(
        run_id=run_id,
        approvals=result.approvals,
        decisions=decisions,
    )
    if repeated is not None:
        return repeated
    async with pending_developer_runs_lock:
        pending_developer_runs[run_id] = PendingAgentRun(
            state=result.state,
            created_monotonic=time.monotonic(),
            agent_model=agent_model,
            reasoning_effort=reasoning_effort,
            vlm_model=vlm_model,
            authorization=authorization,
            approval_decisions=decisions,
        )
    return {
        "status": "approval_required",
        "run_id": run_id,
        "answer": None,
        "approvals": result.approvals,
    }


@app.post("/api/dev/run")
async def run_developer_prompt(request: PromptRequest) -> dict[str, Any]:
    run_id = str(uuid.uuid4())
    agent_model, reasoning_effort, vlm_model = _model_selection(request)
    authorization = _session_authorization(request)
    try:
        return await _developer_agent_step(
            request.prompt,
            run_id,
            agent_model=agent_model,
            reasoning_effort=reasoning_effort,
            vlm_model=vlm_model,
            authorization=authorization,
        )
    except httpx.HTTPStatusError as error:
        detail = error.response.text or str(error)
        raise HTTPException(status_code=502, detail=detail) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@app.post("/api/dev/runs/{run_id}/decision")
async def decide_developer_run(
    run_id: str,
    decision: DeveloperApprovalDecision,
) -> dict[str, Any]:
    async with pending_developer_runs_lock:
        entry = pending_developer_runs.pop(run_id, None)
        expired = [
            pending_id
            for pending_id, pending in pending_developer_runs.items()
            if time.monotonic() - pending.created_monotonic > 600.0
        ]
        for pending_id in expired:
            pending_developer_runs.pop(pending_id, None)
    if entry is None:
        raise HTTPException(
            status_code=404,
            detail="pending developer agent run was not found or expired",
        )
    if time.monotonic() - entry.created_monotonic > 600.0:
        raise HTTPException(
            status_code=410,
            detail="pending developer agent approval expired",
        )
    interruptions = entry.state.get_interruptions()
    if not interruptions:
        raise HTTPException(
            status_code=409,
            detail="developer agent run has no pending approvals",
        )
    try:
        _validate_automatic_agent_approval(interruptions, decision)
    except HTTPException:
        async with pending_developer_runs_lock:
            pending_developer_runs[run_id] = entry
        raise
    approval_decisions = _record_approval_decisions(
        interruptions,
        approved=decision.approve,
        existing=entry.approval_decisions,
    )
    for interruption in interruptions:
        if decision.approve:
            entry.state.approve(interruption)
        else:
            entry.state.reject(
                interruption,
                rejection_message=decision.rejection_message,
            )
    try:
        return await _developer_agent_step(
            entry.state,
            run_id,
            agent_model=entry.agent_model,
            reasoning_effort=entry.reasoning_effort,
            vlm_model=entry.vlm_model,
            authorization=entry.authorization,
            approval_decisions=approval_decisions,
        )
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


@app.post("/api/rgbd-alignment/validate")
async def validate_rgbd_alignment(
    request: RgbdAlignmentRequest,
) -> dict[str, Any]:
    try:
        return await operation_registry.run(
            "rgbd_alignment_validation",
            rgbd_alignment_skill.run(request.request),
            hard_timeout_s=phase4_policy.operation_hard_timeout_s,
            idle_timeout_s=phase4_policy.operation_idle_timeout_s,
        )
    except httpx.HTTPStatusError as error:
        detail = error.response.text or str(error)
        raise HTTPException(status_code=502, detail=detail) from error
    except Exception as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.post("/api/rgbd-alignment/capture-for-review")
async def capture_rgbd_alignment_for_review(
    request: RgbdAlignmentRequest,
) -> dict[str, Any]:
    try:
        return await operation_registry.run(
            "rgbd_alignment_builtin_review_capture",
            rgbd_alignment_skill.capture_for_builtin_review(
                request.request
            ),
            hard_timeout_s=phase4_policy.operation_hard_timeout_s,
            idle_timeout_s=phase4_policy.operation_idle_timeout_s,
        )
    except httpx.HTTPStatusError as error:
        detail = error.response.text or str(error)
        raise HTTPException(status_code=502, detail=detail) from error
    except Exception as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.get("/api/latest-rgbd-alignment-composite")
async def latest_rgbd_alignment_composite() -> Response:
    result = rgbd_alignment_skill.last_result
    if not isinstance(result, dict):
        raise HTTPException(
            status_code=404,
            detail="no RGB-D alignment validation has completed",
        )
    artifacts = result.get("artifacts")
    path_value = artifacts.get("composite") if isinstance(artifacts, dict) else None
    if not isinstance(path_value, str) or not path_value:
        raise HTTPException(
            status_code=404,
            detail="latest validation has no composite artifact",
        )
    path = Path(path_value).resolve()
    artifact_root = settings.screenshot_dir.resolve()
    if path.parent != artifact_root or not path.is_file():
        raise HTTPException(
            status_code=404,
            detail="latest composite artifact is unavailable",
        )
    return Response(
        content=path.read_bytes(),
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/latest-image")
async def latest_image() -> Response:
    try:
        captured = await capture.capture_latest()
        return Response(
            content=captured.image_bytes,
            media_type=captured.mime_type,
            headers={"Cache-Control": "no-store"},
        )
    except Exception as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.get("/api/latest-depth")
async def latest_depth() -> Response:
    try:
        captured = await depth_capture.capture_latest()
        return Response(
            content=captured.image_bytes,
            media_type=captured.mime_type,
            headers={"Cache-Control": "no-store"},
        )
    except Exception as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


def main() -> None:
    uvicorn.run(app, host=settings.ui_host, port=settings.ui_port, log_level="info")


PAGE = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Physical Agent Test Scaffold</title>
  <style>
    :root {
      font-family: Inter, Segoe UI, sans-serif;
      color-scheme: dark;
      --mb-bg: #090909;
      --mb-surface: #131313;
      --mb-surface-raised: #1c1c1c;
      --mb-border: #3b3b3b;
      --mb-text: #f2f2f2;
      --mb-muted: #a8a8a8;
      --mb-accent: #e5e5e5;
      --mb-secondary: #bdbdbd;
      --mb-warning: #f2b84b;
      --mb-danger: #f17878;
      --mb-experimental: #cfcfcf;
    }
    * { box-sizing: border-box; }
    [hidden] { display: none !important; }
    body {
      margin: 0;
      background:
        radial-gradient(circle at 18% -10%, rgba(255,255,255,.08), transparent 34rem),
        radial-gradient(circle at 92% 4%, rgba(255,255,255,.035), transparent 28rem),
        var(--mb-bg);
      color: var(--mb-text);
    }
    main { max-width: 1480px; margin: 0 auto; padding: 24px; }
    h1 { margin-bottom: 4px; }
    h2 { margin-top: 4px; }
    .sub { color: var(--mb-muted); margin-top: 0; }
    .role-kicker { color: var(--mb-accent); font-size: 11px; font-weight: 800; letter-spacing: .16em; text-transform: uppercase; margin-bottom: 6px; }
    .grid { display: grid; grid-template-columns: minmax(360px, 0.8fr) minmax(520px, 1.2fr); gap: 18px; }
    .card { background: rgba(19,19,19,.96); border: 1px solid var(--mb-border); border-radius: 14px; padding: 16px; margin-bottom: 18px; }
    .model-controls { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin: 10px 0 12px; }
    .model-controls label { color: var(--mb-muted); font-size: 11px; }
    .authorization-controls { display: grid; gap: 9px; margin-top: 14px; border-top: 1px solid var(--mb-border); padding-top: 13px; }
    .authorization-toggle { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; margin: 0; color: var(--mb-secondary); font-size: 12px; font-weight: 500; }
    .authorization-toggle input[type="checkbox"] { width: 16px; height: 16px; accent-color: var(--mb-accent); }
    .authorization-toggle input[type="number"] { width: 72px; border: 1px solid var(--mb-border); border-radius: 6px; padding: 6px 7px; background: var(--mb-bg); color: var(--mb-text); font: inherit; }
    .authorization-ticker { margin: 2px 0 0; color: var(--mb-muted); font-size: 11px; line-height: 1.5; }
    select { width: 100%; margin-top: 5px; padding: 8px; border: 1px solid var(--mb-border); border-radius: 7px; background: var(--mb-bg); color: var(--mb-text); }
    textarea { width: 100%; min-height: 100px; resize: vertical; padding: 12px; border-radius: 8px; border: 1px solid var(--mb-border); background: var(--mb-bg); color: var(--mb-text); }
    button { margin-top: 10px; padding: 10px 14px; border: 0; border-radius: 8px; cursor: pointer; font-weight: 600; }
    button.primary { background: var(--mb-accent); color: #111; }
    button.secondary { background: var(--mb-surface-raised); color: var(--mb-text); margin-left: 8px; }
    button.danger { background: #6f2f38; color: #ffe9eb; margin-left: 8px; }
    button:disabled { opacity: .55; cursor: progress; }
    pre { white-space: pre-wrap; word-break: break-word; background: #101010; padding: 12px; border-radius: 8px; min-height: 72px; max-height: 520px; overflow: auto; }
    img { width: 100%; min-height: 220px; object-fit: contain; background: #050505; border-radius: 8px; }
    .sensor-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
    .catalog-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
    .catalog-list { display: grid; gap: 7px; }
    .catalog-item { display: flex; justify-content: space-between; gap: 12px; padding: 9px 10px; border: 1px solid var(--mb-border); border-radius: 8px; background: #101010; }
    .catalog-item a { color: var(--mb-text); text-decoration: none; overflow-wrap: anywhere; }
    .catalog-item span { color: var(--mb-muted); font-size: 11px; white-space: nowrap; }
    details { margin-top: 12px; color: var(--mb-muted); }
    details summary { cursor: pointer; }
    .status { font-size: 12px; color: #d1d1d1; }
    .viewer-wrap { position: relative; width: 100%; height: min(68vh, 720px); min-height: 430px; background: #050505; border-radius: 10px; overflow: hidden; }
    #cloud { display: block; width: 100%; height: 100%; cursor: grab; }
    #cloud:active { cursor: grabbing; }
    .viewer-overlay { position: absolute; left: 10px; top: 10px; background: rgba(5,5,5,.78); padding: 7px 9px; border-radius: 6px; font-size: 12px; pointer-events: none; }
    .gravity-overlay { position: absolute; right: 10px; top: 10px; background: rgba(5,5,5,.82); padding: 7px 9px; border-radius: 6px; font-size: 12px; pointer-events: none; text-align: right; white-space: pre-line; }
    .axis-overlay { position: absolute; left: 10px; bottom: 10px; display: grid; grid-template-columns: auto auto; gap: 3px 9px; background: rgba(5,5,5,.82); padding: 8px 10px; border-radius: 6px; font-size: 12px; pointer-events: none; }
    .axis-overlay strong { font-variant-numeric: tabular-nums; }
    .world-x { color: #ff5a5f; }
    .world-y { color: #58d68d; }
    .world-z { color: #4da3ff; }
    .world-down { color: #ff7a45; }
    .axis-toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-bottom: 10px; }
    .axis-toolbar button { margin: 0; }
    .axis-toolbar .controls { margin: 0 0 0 auto; }
    .axis-controls { display: grid; grid-template-columns: repeat(auto-fit, minmax(245px, 1fr)); gap: 7px; margin: 0 0 10px; }
    .axis-item { border: 1px solid var(--mb-border); border-radius: 8px; background: #101010; padding: 7px 9px; min-width: 0; }
    .axis-item.unavailable { opacity: .58; }
    .axis-row { display: flex; align-items: center; gap: 7px; min-width: 0; }
    .axis-row input { margin: 0; flex: 0 0 auto; }
    .axis-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; font-weight: 700; }
    .axis-role { margin-left: auto; color: var(--mb-muted); font-size: 9px; letter-spacing: .05em; white-space: nowrap; }
    .axis-item details { margin: 5px 0 0 23px; font-size: 11px; }
    .axis-item details pre { margin: 5px 0 0; min-height: 0; max-height: 160px; padding: 8px; font-size: 10px; }
    .frame-label-layer { position: absolute; inset: 0; pointer-events: none; overflow: hidden; }
    .frame-label { position: absolute; transform: translate(6px, -50%); padding: 2px 5px; border-radius: 4px; background: rgba(5,5,5,.84); color: #f2f2f2; font-size: 10px; white-space: nowrap; border: 1px solid rgba(255,255,255,.18); }
    .screen-axis-overlay { position: absolute; right: 10px; bottom: 10px; width: 184px; height: 116px; border-radius: 8px; background: rgba(5,5,5,.88); border: 1px solid rgba(255,255,255,.18); pointer-events: none; }
    .screen-axis-overlay svg { width: 100%; height: 100%; }
    .screen-axis-overlay text { fill: #d7d7d7; font: 10px system-ui, sans-serif; }
    body.spatial-inspector-mode main { max-width: 1900px; }
    body.spatial-inspector-mode .grid { grid-template-columns: 1fr; }
    body.spatial-inspector-mode .grid > div:first-child,
    body.spatial-inspector-mode #spaceCognitionLinkPanel { display: none; }
    body.spatial-inspector-mode .viewer-wrap { height: min(76vh, 960px); min-height: 560px; }
    .init-summary { margin-top: 10px; padding: 9px 10px; background: #181818; border-radius: 8px; color: #d1d1d1; font-size: 13px; }
    .state-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin: 10px 0; }
    .state-card { border: 1px solid #444; border-radius: 8px; background: #181818; padding: 9px 10px; min-height: 58px; }
    .state-label { color: #a8a8a8; font-size: 11px; text-transform: uppercase; letter-spacing: .06em; }
    .state-value { margin-top: 3px; font-size: 14px; font-weight: 700; color: #e5e5e5; display: flex; align-items: center; gap: 7px; }
    .state-lamp { width: 10px; height: 10px; border-radius: 50%; background: #777; box-shadow: 0 0 0 2px rgba(119,119,119,.18); flex: 0 0 auto; }
    .state-lamp.ok { background: #22c55e; box-shadow: 0 0 8px rgba(34,197,94,.75); }
    .state-lamp.warn { background: #f59e0b; box-shadow: 0 0 8px rgba(245,158,11,.72); }
    .state-lamp.bad { background: #ef4444; box-shadow: 0 0 8px rgba(239,68,68,.75); }
    .state-lamp.busy { background: #e5e5e5; box-shadow: 0 0 8px rgba(229,229,229,.55); }
    .state-detail { margin-top: 2px; color: #a8a8a8; font-size: 11px; }
    .state-card.ok { border-color: #166534; }
    .state-card.ok .state-value { color: #86efac; }
    .state-card.warn { border-color: #92400e; }
    .state-card.warn .state-value { color: #fde68a; }
    .state-card.bad { border-color: #991b1b; }
    .state-card.bad .state-value { color: #fca5a5; }
    .state-card.busy { border-color: #777; }
    .state-card.busy .state-value { color: #f2f2f2; }
    .action-status { margin-top: 8px; color: #d0d0d0; font-size: 12px; min-height: 18px; }
    .controls { color: #a8a8a8; font-size: 12px; margin: 8px 0 0; }
    dialog { width: min(680px, calc(100vw - 28px)); color: var(--mb-text); background: var(--mb-surface); border: 1px solid var(--mb-accent); border-radius: 16px; padding: 0; box-shadow: 0 30px 90px rgba(0,0,0,.55); }
    dialog::backdrop { background: rgba(0,0,0,.82); backdrop-filter: blur(4px); }
    .decision-head { padding: 18px 20px 12px; border-bottom: 1px solid var(--mb-border); }
    .decision-body { padding: 18px 20px; }
    .decision-body pre { max-height: 260px; }
    .decision-actions { display: flex; justify-content: flex-end; gap: 10px; padding: 0 20px 20px; }
    .decision-actions button { margin: 0; }
    .decision-note { color: var(--mb-warning); font-size: 12px; }
    @media (max-width: 980px) { .grid { grid-template-columns: 1fr; } .viewer-wrap { height: 55vh; } }
    @media (max-width: 640px) { .sensor-grid, .catalog-grid, .model-controls { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
<main>
  <nav style="display:flex;justify-content:space-between;gap:16px;margin-bottom:24px">
    <a href="http://127.0.0.1:7001/" style="color:var(--mb-muted);text-decoration:none">← Midbrain</a>
    <a id="secondaryNav" href="/" style="color:var(--mb-warning);text-decoration:none">Regular agent</a>
  </nav>
  <div class="role-kicker">Development UI</div>
  <h1 id="pageTitle">Physical Agent Developer</h1>
  <p class="sub" id="pageSubtitle">All adapter-bound Skills, Provider lifecycle tools, relative IK, controller-owned safe-home, selectable models, and detailed development controls</p>
  <div class="grid">
    <div>
      <section class="card" id="spaceCognitionPanel">
        <div class="role-kicker">Development controls</div>
        <h2>Space cognition</h2>
        <div class="state-grid">
          <div class="state-card" id="visualState"><div class="state-label">Visual correction</div><div class="state-value"><span class="state-lamp"></span><span class="state-text">UNKNOWN</span></div><div class="state-detail">RGB-D/IR updates correct inertial drift</div></div>
          <div class="state-card" id="poseState"><div class="state-label">Pose propagation</div><div class="state-value"><span class="state-lamp"></span><span class="state-text">UNKNOWN</span></div><div class="state-detail">IMU is the primary motion clock</div></div>
          <div class="state-card" id="rotationState"><div class="state-label">Rotation estimator</div><div class="state-value"><span class="state-lamp"></span><span class="state-text">UNKNOWN</span></div><div class="state-detail">Gyro propagation with visual error-state correction</div></div>
          <div class="state-card" id="gravityState"><div class="state-label">Gravity adjustment</div><div class="state-value"><span class="state-lamp"></span><span class="state-text">OFF</span></div><div class="state-detail">Quiet IMU enables slow roll/pitch leveling</div></div>
          <div class="state-card" id="featureState"><div class="state-label">Feature extraction</div><div class="state-value"><span class="state-lamp"></span><span class="state-text">UNKNOWN</span></div><div class="state-detail">Raw baseline always remains available</div></div>
          <div class="state-card" id="mapState"><div class="state-label">Map capture</div><div class="state-value"><span class="state-lamp"></span><span class="state-text">UNKNOWN</span></div><div class="state-detail">Waiting for RGB-D</div></div>
          <div class="state-card" id="initState"><div class="state-label">Initialization / reset</div><div class="state-value"><span class="state-lamp"></span><span class="state-text">UNKNOWN</span></div><div class="state-detail">No result yet</div></div>
        </div>
        <button class="primary" id="initialize">Initialize / verify</button>
        <button class="danger" id="reset">Force reinitialize origin</button>
        <button class="secondary" id="clearCloud">Clear point cloud</button>
        <div class="action-status" id="actionStatus">No manual action running.</div>
        <div class="init-summary" id="initSummary">Automatic startup initialization has not reported yet.</div>
        <pre id="spaceStatus" class="status">Loading…</pre>
      </section>
      <section class="card" id="agentPromptPanel">
        <div class="role-kicker">Agent observation</div>
        <h2>Prompt</h2>
        <div class="model-controls">
          <label>Agent model<select id="agentModel"><option value="gpt-5.6-terra">GPT-5.6 Terra</option></select></label>
          <label>Reasoning<select id="reasoningEffort"><option value="medium">Medium</option></select></label>
          <label>Visual model<select id="vlmModel"><option value="auto">Auto routing</option></select></label>
        </div>
        <textarea id="prompt" placeholder="Describe the task for the agent."></textarea>
        <div>
          <button class="primary" id="run">Run prompt</button>
        </div>
        <div class="authorization-controls" aria-label="Session authorization">
          <label class="authorization-toggle">
            <input id="autoApproveProviders" type="checkbox" checked>
            Auto-authorize Provider start, HOT, and WARM transitions
          </label>
          <label class="authorization-toggle">
            <input id="autoApproveMoves" type="checkbox" checked>
            Auto-authorize each exact relative arm pose preview: translation up to
            <input id="maxAutoMoveCm" type="number" min="0.1" max="100" step="0.1" value="35" inputmode="decimal" aria-label="Maximum automatically authorized move in centimeters">
            cm at a nominal average speed up to
            <input id="maxAutoSpeedMps" type="number" min="0.001" max="0.5" step="0.001" value="0.5" inputmode="decimal" aria-label="Maximum automatically authorized nominal speed in meters per second">
            m/s; controlled-frame yaw up to 45°
          </label>
          <label class="authorization-toggle">
            <input id="autoApproveCalibration" type="checkbox" checked>
            Auto-authorize stationary world-to-arm calibration
          </label>
          <label class="authorization-toggle">
            <input id="autoApproveCalibrationActivation" type="checkbox" checked>
            Auto-authorize exact qualified calibration candidate activation
          </label>
          <p id="authorizationTicker" class="authorization-ticker"></p>
        </div>
        <h2>Answer</h2>
        <pre id="answer">Ready.</pre>
      </section>
      <section class="card" id="replayPanel">
        <div class="role-kicker">Replay provenance</div>
        <h2>Hardware-isolated capture bundles</h2>
        <p class="sub">Read-only provenance: payload hashes, provider boot identity, calibration/VIO context, evidence coverage, and manual retention review. This surface cannot start hardware or call a controller.</p>
        <button class="secondary" id="refreshReplay">Refresh provenance</button>
        <pre id="replayProvenance">Loading replay bundles...</pre>
      </section>
      <section class="card" id="platformCatalogPanel">
        <div class="role-kicker">Diagnostics</div>
        <h2>Installed components</h2>
        <p class="sub">Compact live catalog from Midbrain. Newly installed Skill manifests appear here and in the main portal without changing this page.</p>
        <div class="catalog-grid">
          <div>
            <h3>Providers <span id="providerCount"></span></h3>
            <div id="providerCatalog" class="catalog-list"></div>
          </div>
          <div>
            <h3>Skills <span id="skillCount"></span></h3>
            <div id="skillCatalog" class="catalog-list"></div>
          </div>
        </div>
        <details>
          <summary>Core runtime details</summary>
          <pre id="status" class="status">Loading…</pre>
        </details>
      </section>
    </div>
    <div>
      <section class="card" id="worldPointCloudPanel">
        <div class="role-kicker">Observation</div>
        <h2>World RGB point cloud</h2>
        <div class="axis-toolbar">
          <button class="secondary" id="resetView">Reset isometric view</button>
          <button class="secondary" id="fitAxes">Fit visible axes</button>
          <span class="controls" id="axisStatus">Loading local coordinate frames…</span>
        </div>
        <div class="axis-controls" id="axisControls" aria-label="Spatial frame visibility"></div>
        <div class="viewer-wrap">
          <canvas id="cloud"></canvas>
          <div class="viewer-overlay" id="cloudStats">Waiting for pose and RGB-D…</div>
          <div class="gravity-overlay" id="gravityStatus">↓ World gravity · -Z</div>
          <div class="frame-label-layer" id="frameLabels" aria-hidden="true"></div>
          <div class="screen-axis-overlay" id="screenAxisOverlay" hidden aria-label="2D screen-space axes">
            <svg viewBox="0 0 184 116" aria-hidden="true">
              <defs>
                <marker id="screenArrowX" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 z" fill="#ff5a5f"/></marker>
                <marker id="screenArrowY" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 z" fill="#58d68d"/></marker>
              </defs>
              <circle cx="30" cy="30" r="3" fill="#f2f2f2"/>
              <line x1="30" y1="30" x2="150" y2="30" stroke="#ff5a5f" stroke-width="2" marker-end="url(#screenArrowX)"/>
              <line x1="30" y1="30" x2="30" y2="88" stroke="#58d68d" stroke-width="2" marker-end="url(#screenArrowY)"/>
              <text x="74" y="21">image +X · right</text>
              <text x="39" y="77">image +Y · down</text>
              <text x="8" y="108">2D screen only · top-left origin</text>
            </svg>
          </div>
          <div class="axis-overlay" aria-label="World coordinate legend">
            <strong class="world-x">+X</strong><span>front</span>
            <strong class="world-y">+Y</strong><span>left</span>
            <strong class="world-z">+Z</strong><span>up</span>
            <strong class="world-down">-Z</strong><span>gravity / down</span>
          </div>
        </div>
        <p class="controls">Orthographic world view. Drag to orbit; Shift-drag, middle-drag, or right-drag to pan; use the wheel to zoom. Frame checkboxes control live local XYZ triads: red +X, green +Y, blue +Z. Orange remains gravity/down (-Z), cyan is the camera frustum, and point-cloud samples fade over 10 seconds.</p>
      </section>
      <section class="card" id="spaceCognitionLinkPanel">
        <div class="role-kicker">Point-cloud recovery</div>
        <h2>Space cognition</h2>
        <p class="sub">If the world point cloud stops updating or the local frame has drifted, inspect Space Cognition before deliberately resetting its origin.</p>
        <a href="/dev/skills/initialize-space-cognition" style="color:var(--mb-warning)">Open Space Cognition development UI →</a>
        <br>
        <a href="/dev/spatial-axes" style="color:var(--mb-warning)">Open Spatial Axis Inspector →</a>
      </section>
    </div>
  </div>
</main>
<dialog id="authorizationDialog" aria-labelledby="authorizationTitle">
  <div class="decision-head">
    <div class="role-kicker">Authorization required</div>
    <h2 id="authorizationTitle">Pending decision</h2>
    <p class="sub" id="authorizationSummary"></p>
  </div>
  <div class="decision-body">
    <p class="decision-note">Approval records this decision only. It does not execute motion or bypass provider safety checks.</p>
    <pre id="authorizationDetails"></pre>
  </div>
  <div class="decision-actions">
    <button class="danger" id="denyAuthorization">Deny</button>
    <button class="primary" id="approveAuthorization">Approve decision</button>
  </div>
</dialog>
<script>
const dedicatedSpaceCognition = location.pathname.endsWith(
  '/dev/skills/initialize-space-cognition'
);
const dedicatedSpatialAxes = location.pathname.endsWith('/dev/spatial-axes');
const focusedUtilityPage = dedicatedSpaceCognition || dedicatedSpatialAxes;
const pageTitle = document.getElementById('pageTitle');
const pageSubtitle = document.getElementById('pageSubtitle');
const secondaryNav = document.getElementById('secondaryNav');
const spaceCognitionLinkPanel = document.getElementById('spaceCognitionLinkPanel');
const spaceCognitionPanel = document.getElementById('spaceCognitionPanel');
const agentPromptPanel = document.getElementById('agentPromptPanel');
const replayPanel = document.getElementById('replayPanel');
const platformCatalogPanel = document.getElementById('platformCatalogPanel');
if (dedicatedSpaceCognition) {
  document.title = 'Space Cognition Development | Midbrain';
  pageTitle.textContent = 'Space Cognition Development';
  pageSubtitle.textContent =
    'Initialize or deliberately re-establish the local spatial epoch, inspect VIO state, and review the accumulated world model.';
  secondaryNav.href = '/dev';
  secondaryNav.textContent = 'Developer agent';
  spaceCognitionLinkPanel.hidden = true;
  agentPromptPanel.hidden = true;
  replayPanel.hidden = true;
  platformCatalogPanel.hidden = true;
} else if (dedicatedSpatialAxes) {
  document.body.classList.add('spatial-inspector-mode');
  document.title = 'Spatial Axis Inspector | Midbrain';
  pageTitle.textContent = 'Spatial Axis Inspector';
  pageSubtitle.textContent =
    'Inspect the live point cloud together with world, robot, camera, joint, object, and explicit 2D screen coordinate frames.';
  secondaryNav.href = '/dev';
  secondaryNav.textContent = 'Developer agent';
} else {
  document.title = 'Developer Agent | Midbrain';
  spaceCognitionPanel.hidden = true;
}
const runButton = document.getElementById('run');
const refreshReplayButton = document.getElementById('refreshReplay');
const initializeButton = document.getElementById('initialize');
const resetButton = document.getElementById('reset');
const clearCloudButton = document.getElementById('clearCloud');
const resetViewButton = document.getElementById('resetView');
const fitAxesButton = document.getElementById('fitAxes');
const axisControls = document.getElementById('axisControls');
const axisStatus = document.getElementById('axisStatus');
const frameLabels = document.getElementById('frameLabels');
const screenAxisOverlay = document.getElementById('screenAxisOverlay');
const authorizationDialog = document.getElementById('authorizationDialog');
const authorizationTitle = document.getElementById('authorizationTitle');
const authorizationSummary = document.getElementById('authorizationSummary');
const authorizationDetails = document.getElementById('authorizationDetails');
const approveAuthorization = document.getElementById('approveAuthorization');
const denyAuthorization = document.getElementById('denyAuthorization');
const promptBox = document.getElementById('prompt');
const answer = document.getElementById('answer');
const agentModel = document.getElementById('agentModel');
const reasoningEffort = document.getElementById('reasoningEffort');
const vlmModel = document.getElementById('vlmModel');
const autoApproveProviders = document.getElementById('autoApproveProviders');
const autoApproveMoves = document.getElementById('autoApproveMoves');
const autoApproveCalibration = document.getElementById('autoApproveCalibration');
const autoApproveCalibrationActivation = document.getElementById('autoApproveCalibrationActivation');
const maxAutoMoveCm = document.getElementById('maxAutoMoveCm');
const maxAutoSpeedMps = document.getElementById('maxAutoSpeedMps');
const authorizationTicker = document.getElementById('authorizationTicker');
const replayProvenance = document.getElementById('replayProvenance');
const statusBox = document.getElementById('status');
const spaceStatus = document.getElementById('spaceStatus');
const initSummary = document.getElementById('initSummary');
const cloudStats = document.getElementById('cloudStats');
const gravityStatus = document.getElementById('gravityStatus');
const visualState = document.getElementById('visualState');
const poseState = document.getElementById('poseState');
const rotationState = document.getElementById('rotationState');
const gravityState = document.getElementById('gravityState');
const featureState = document.getElementById('featureState');
const mapState = document.getElementById('mapState');
const initState = document.getElementById('initState');
const actionStatus = document.getElementById('actionStatus');
let cloudCaptureState = 'unknown';
let latestCameraPose = null;
let activeAuthorizationId = null;
const AGENT_AUTHORIZATION_STORAGE_KEY =
  'midbrain.developerAgent.sessionAuthorization.v1';

function loadAgentAuthorizationPreferences() {
  let saved = {};
  try {
    saved = JSON.parse(
      window.sessionStorage.getItem(AGENT_AUTHORIZATION_STORAGE_KEY) || '{}'
    );
  } catch (_error) {
    saved = {};
  }
  autoApproveProviders.checked =
    typeof saved.autoApproveProviders === 'boolean'
      ? saved.autoApproveProviders
      : true;
  autoApproveMoves.checked =
    typeof saved.autoApproveMoves === 'boolean'
      ? saved.autoApproveMoves
      : true;
  autoApproveCalibration.checked =
    typeof saved.autoApproveCalibration === 'boolean'
      ? saved.autoApproveCalibration
      : true;
  autoApproveCalibrationActivation.checked =
    typeof saved.autoApproveCalibrationActivation === 'boolean'
      ? saved.autoApproveCalibrationActivation
      : true;
  const savedMaximum = Number(saved.maxAutoMoveCm);
  maxAutoMoveCm.value =
    Number.isFinite(savedMaximum) && savedMaximum >= 0.1 &&
    savedMaximum <= 100 ? String(savedMaximum) : '35';
  const savedMaximumSpeed = Number(saved.maxAutoSpeedMps);
  maxAutoSpeedMps.value =
    Number.isFinite(savedMaximumSpeed) && savedMaximumSpeed > 0 &&
    savedMaximumSpeed <= 0.5 ? String(savedMaximumSpeed) : '0.5';
  updateAgentAuthorizationTicker();
}

function agentAuthorizationPreferences() {
  const maximum = Number(maxAutoMoveCm.value);
  const maximumSpeed = Number(maxAutoSpeedMps.value);
  return {
    autoApproveProviders: autoApproveProviders.checked,
    autoApproveMoves: autoApproveMoves.checked,
    autoApproveCalibration: autoApproveCalibration.checked,
    autoApproveCalibrationActivation:
      autoApproveCalibrationActivation.checked,
    maxAutoMoveCm:
      Number.isFinite(maximum) && maximum >= 0.1 && maximum <= 100
        ? maximum
        : 35,
    maxAutoSpeedMps:
      Number.isFinite(maximumSpeed) && maximumSpeed > 0 &&
      maximumSpeed <= 0.5 ? maximumSpeed : 0.5
  };
}

function updateAgentAuthorizationTicker() {
  const preferences = agentAuthorizationPreferences();
  maxAutoMoveCm.disabled = !preferences.autoApproveMoves;
  maxAutoSpeedMps.disabled = !preferences.autoApproveMoves;
  const providerState = preferences.autoApproveProviders
    ? 'Provider activation AUTO'
    : 'Provider activation asks';
  const motionState = preferences.autoApproveMoves
    ? 'relative arm pose AUTO <= ' + preferences.maxAutoMoveCm + ' cm, <= ' +
      preferences.maxAutoSpeedMps + ' m/s nominal average; ' +
      'controlled-frame yaw AUTO <= 45°'
    : 'physical motion asks';
  const calibrationState = preferences.autoApproveCalibration
    ? 'world-arm calibration AUTO'
    : 'world-arm calibration asks';
  const activationState = preferences.autoApproveCalibrationActivation
    ? 'exact calibration activation AUTO'
    : 'exact calibration activation asks';
  authorizationTicker.textContent =
    providerState + ' | ' + motionState + ' | ' + calibrationState + ' | ' +
    activationState + '. Provider and controller safety checks remain active; ' +
    'stop, safe-home, and other protected actions still ask.';
  try {
    window.sessionStorage.setItem(
      AGENT_AUTHORIZATION_STORAGE_KEY,
      JSON.stringify(preferences)
    );
  } catch (_error) {
    // The controls remain valid for this page even if storage is disabled.
  }
}

function agentApprovalArguments(approval) {
  const raw = (approval.request && approval.request.arguments) || {};
  if (typeof raw === 'string') {
    try {
      const decoded = JSON.parse(raw);
      return decoded && typeof decoded === 'object' ? decoded : {};
    } catch (_error) {
      return {};
    }
  }
  return raw && typeof raw === 'object' ? raw : {};
}

function automaticDeveloperApprovalDecision(approvals) {
  if (!approvals.length) return null;
  const preferences = agentAuthorizationPreferences();
  const providerEligible =
    preferences.autoApproveProviders &&
    approvals.every((approval) => {
      const action = String(agentApprovalArguments(approval).action || '')
        .toLowerCase();
      return approval.tool_name === 'set_provider_residency' &&
        ['start', 'hot', 'warm'].includes(action);
    });
  if (providerEligible) {
    return {
      approval_mode: 'AUTO_PROVIDER_ACTIVATION',
      max_auto_move_cm: null,
      max_auto_speed_m_s: null,
      label: 'Session authorization automatically approved Provider activation.'
    };
  }
  const motionEligible =
    preferences.autoApproveMoves &&
    approvals.every((approval) => {
      const argumentsValue = agentApprovalArguments(approval);
      const motionIntent = String(argumentsValue.motion_intent || '')
        .toUpperCase();
      const direction = String(argumentsValue.direction || '')
        .toUpperCase();
      const orientationPolicy = String(
        argumentsValue.orientation_policy || ''
      ).toUpperCase();
      const distanceM = Number(argumentsValue.distance_m);
      const plannedSpeedMps = Number(
        argumentsValue.planned_nominal_speed_m_s
      );
      const rawYawDegrees = argumentsValue.controlled_frame_yaw_delta_deg;
      const hasYaw = rawYawDegrees !== null && rawYawDegrees !== undefined;
      const yawDegrees = Number(rawYawDegrees);
      const boundedTranslation =
        Number.isFinite(distanceM) && distanceM > 0 &&
        distanceM * 100 <= preferences.maxAutoMoveCm + 1e-9 &&
        Number.isFinite(plannedSpeedMps) && plannedSpeedMps > 0 &&
        plannedSpeedMps <= preferences.maxAutoSpeedMps + 1e-9;
      const boundedYaw = hasYaw && Number.isFinite(yawDegrees) &&
        Math.abs(yawDegrees) > 1e-9 &&
        Math.abs(yawDegrees) <= 45 + 1e-9 &&
        orientationPolicy === 'APPLY_CONTROLLED_FRAME_YAW_DELTA';
      const exactTranslation = motionIntent === 'NEW_RELATIVE_MOVE' &&
        boundedTranslation && !hasYaw;
      const exactCombinedPose =
        motionIntent === 'NEW_RELATIVE_POSE_MOVE' &&
        direction !== 'NONE' && boundedTranslation && boundedYaw;
      const exactPureRotation =
        motionIntent === 'NEW_RELATIVE_ROTATION' &&
        direction === 'NONE' && distanceM === 0 &&
        plannedSpeedMps === 0 &&
        argumentsValue.requested_speed_m_s == null && boundedYaw;
      return approval.tool_name === 'execute_integrated_motion_preview' &&
        (exactTranslation || exactCombinedPose || exactPureRotation);
    });
  if (motionEligible) {
    return {
      approval_mode: 'AUTO_BOUNDED_RELATIVE_MOTION',
      max_auto_move_cm: preferences.maxAutoMoveCm,
      max_auto_speed_m_s: preferences.maxAutoSpeedMps,
      label:
        'Session authorization automatically approved the bounded exact motion preview.'
    };
  }
  const calibrationEligible =
    preferences.autoApproveCalibration &&
    approvals.every(
      (approval) => approval.tool_name === 'calibrate_stationary_workcell'
    );
  if (calibrationEligible) {
    return {
      approval_mode: 'AUTO_STATIONARY_CALIBRATION',
      max_auto_move_cm: null,
      max_auto_speed_m_s: null,
      label:
        'Session authorization automatically approved stationary world-to-arm calibration.'
    };
  }
  const activationEligible =
    preferences.autoApproveCalibrationActivation &&
    approvals.every(
      (approval) =>
        approval.tool_name === 'review_and_activate_stationary_calibration'
    );
  if (activationEligible) {
    return {
      approval_mode: 'AUTO_STATIONARY_ACTIVATION',
      max_auto_move_cm: null,
      max_auto_speed_m_s: null,
      label:
        'Session authorization automatically approved exact stationary calibration activation.'
    };
  }
  return null;
}

async function refreshAuthorization() {
  if (focusedUtilityPage) return;
  try {
    const response = await fetch('/api/authorizations?status=PENDING', {cache: 'no-store'});
    if (!response.ok) return;
    const data = await response.json();
    const pending = data.authorizations || [];
    if (!pending.length) {
      activeAuthorizationId = null;
      if (authorizationDialog.open) authorizationDialog.close();
      return;
    }
    const decision = pending[0];
    if (activeAuthorizationId === decision.decision_id && authorizationDialog.open) return;
    activeAuthorizationId = decision.decision_id;
    authorizationTitle.textContent = decision.title;
    authorizationSummary.textContent = decision.summary;
    authorizationDetails.textContent = JSON.stringify({
      requester: {type: decision.requester_type, id: decision.requester_id},
      decision_type: decision.decision_type,
      proposed_action: decision.proposed_action,
      evidence: decision.evidence,
      safety: decision.safety,
      expires_at_us: decision.expires_at_us
    }, null, 2);
    if (!authorizationDialog.open) authorizationDialog.showModal();
  } catch (_error) {
    return;
  }
}

async function refreshReplayProvenance() {
  if (focusedUtilityPage) return;
  refreshReplayButton.disabled = true;
  try {
    const response = await fetch('/api/phase5/replay/bundles', {cache: 'no-store'});
    if (!response.ok) throw new Error(await response.text());
    const data = await response.json();
    replayProvenance.textContent = JSON.stringify({
      hardware_access_allowed: data.hardware_access_allowed,
      bundles: (data.bundles || []).map((bundle) => ({
        bundle_id: bundle.bundle_id,
        status: bundle.status,
        created_at_us: bundle.created_at_us,
        provenance: bundle.provenance
      }))
    }, null, 2);
  } catch (error) {
    replayProvenance.textContent = 'Replay provenance unavailable: ' + error.message;
  } finally {
    refreshReplayButton.disabled = false;
  }
}

async function resolveAuthorization(resolution) {
  if (!activeAuthorizationId) return;
  approveAuthorization.disabled = true;
  denyAuthorization.disabled = true;
  try {
    const response = await fetch('/api/authorizations/' + encodeURIComponent(activeAuthorizationId) + '/resolve', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({resolution, resolved_by: 'browser-local-operator'})
    });
    if (!response.ok) throw new Error(await response.text());
    activeAuthorizationId = null;
    authorizationDialog.close();
  } finally {
    approveAuthorization.disabled = false;
    denyAuthorization.disabled = false;
  }
}

approveAuthorization.addEventListener('click', () => resolveAuthorization('APPROVED'));
denyAuthorization.addEventListener('click', () => resolveAuthorization('DENIED'));

function setStateCard(element, value, detail, kind) {
  element.className = 'state-card' + (kind ? ' ' + kind : '');
  const textElement = element.querySelector('.state-text');
  if (textElement) textElement.textContent = value;
  const lamp = element.querySelector('.state-lamp');
  if (lamp) lamp.className = 'state-lamp' + (kind ? ' ' + kind : '');
  element.querySelector('.state-detail').textContent = detail;
}

function stateKind(value) {
  const text = String(value || '').toUpperCase();
  if (['TRACKING', 'CAPTURING', 'SUCCEEDED', 'ALIGNED'].includes(text)) return 'ok';
  if (text.includes('RUNNING') || text.includes('SCHEDULED') || text.includes('INITIALIZ') || text.includes('RECOVERY') || text.includes('TRIM')) return 'busy';
  if (text.includes('FAILED') || text.includes('ERROR')) return 'bad';
  if (text.includes('DEGRADED') || text.includes('PAUSED') || text.includes('WAITING') || text.includes('DRIFT') || text.includes('MOVING')) return 'warn';
  return '';
}

function populateModelSelect(select, values, selectedValue, labeler) {
  if (select.dataset.loaded === 'true') return;
  select.replaceChildren(...values.map((value) => {
    const option = document.createElement('option');
    option.value = value;
    option.textContent = labeler(value);
    return option;
  }));
  select.value = values.includes(selectedValue) ? selectedValue : values[0];
  select.dataset.loaded = 'true';
}

function renderComponentCatalog(targetId, items) {
  const target = document.getElementById(targetId);
  target.replaceChildren();
  for (const item of items) {
    const row = document.createElement('div');
    row.className = 'catalog-item';
    const link = document.createElement('a');
    const state = document.createElement('span');
    link.textContent = item.display_name || item.id || 'Unknown component';
    link.href = 'http://127.0.0.1:7001' + (item.observation_url || '/');
    link.target = '_blank';
    link.rel = 'noreferrer';
    state.textContent = item.status || 'UNKNOWN';
    row.append(link, state);
    target.append(row);
  }
  if (!items.length) {
    const empty = document.createElement('div');
    empty.className = 'catalog-item';
    empty.textContent = 'None discovered';
    target.append(empty);
  }
}

async function refreshStatus() {
  try {
    const response = await fetch('/api/status', {cache: 'no-store'});
    const data = await response.json();
    populateModelSelect(
      agentModel,
      data.agent_model_options || [data.openai_model],
      data.openai_model,
      (value) => value.replace('gpt-5.6-', 'GPT-5.6 ')
    );
    populateModelSelect(
      reasoningEffort,
      data.agent_reasoning_options || ['medium'],
      data.agent_reasoning_effort || 'medium',
      (value) => value.toUpperCase()
    );
    populateModelSelect(
      vlmModel,
      data.vlm_model_options || ['auto'],
      'auto',
      (value) => value === 'auto' ? 'Auto routing' : value
    );
    const initializationData = (data.space_cognition && data.space_cognition.data) || {};
    const vioData = (data.vio && data.vio.data) || {};
    const poseData = (data.body_pose && data.body_pose.data) || {};
    const cloudData = data.world_point_cloud || {};
    const systemCatalog = data.system_catalog || {};
    const catalogProviders = Array.isArray(systemCatalog.providers)
      ? systemCatalog.providers
      : [];
    const catalogSkills = Array.isArray(systemCatalog.skills)
      ? systemCatalog.skills
      : [];
    renderComponentCatalog('providerCatalog', catalogProviders);
    renderComponentCatalog('skillCatalog', catalogSkills);
    document.getElementById('providerCount').textContent =
      '(' + catalogProviders.length + ')';
    document.getElementById('skillCount').textContent =
      '(' + catalogSkills.length + ')';
    cloudCaptureState = cloudData.capture_state || 'unknown';
    latestCameraPose = poseData.world_from_camera || null;
    updateCameraMarker(latestCameraPose);
    const epoch = initializationData.session_epoch || vioData.session_epoch || 'none';
    const currentSkillState = initializationData.state || 'not published';
    const displayedAutoState = (
      data.auto_initialize_state === 'FAILED' &&
      (currentSkillState === 'RUNNING' || currentSkillState === 'SUCCEEDED')
    ) ? 'SUPERSEDED' : data.auto_initialize_state;
    initSummary.textContent = [
      'Startup auto-init: ' + displayedAutoState,
      'skill: ' + currentSkillState,
      'VIO: ' + (vioData.tracking_state || 'unknown'),
      'epoch: ' + String(epoch).slice(0, 12)
    ].join(' · ');
    const tilt = vioData.gravity_tilt_error_rad;
    const tiltDegrees = typeof tilt === 'number' ? (tilt * 180 / Math.PI).toFixed(2) : 'n/a';
    const visualTracking = vioData.tracking_state || 'UNKNOWN';
    const poseMode = vioData.pose_update_mode || 'UNKNOWN';
    const visualAccepted = Boolean(vioData.visual_update_accepted);
    const visualSensor = vioData.visual_sensor || 'NONE';
    const visualStale = typeof vioData.visual_stale_s === 'number' ? vioData.visual_stale_s : null;
    const propagationSteps = Number(vioData.imu_propagation_steps || 0);
    const accelHistoryCount = Number(vioData.imu_accelerometer_history_count || 0);
    const gyroHistoryCount = Number(vioData.imu_gyroscope_history_count || 0);
    const imuTimestampSkewUs = vioData.imu_timestamp_skew_us;
    const initializationBlocker = vioData.initialization_blocker || 'none';
    const initAccelWindowCount = Number(vioData.initialization_accelerometer_window_count || 0);
    const initGyroWindowCount = Number(vioData.initialization_gyroscope_window_count || 0);
    const initAccelRateHz = vioData.initialization_accelerometer_rate_hz;
    const initGyroRateHz = vioData.initialization_gyroscope_rate_hz;
    const rotationSource = vioData.rotation_source || 'UNKNOWN';
    const rotationDisagreement = typeof vioData.rotation_disagreement_rad === 'number'
      ? vioData.rotation_disagreement_rad
      : null;
    const gyroRotationAngle = typeof vioData.gyro_rotation_angle_rad === 'number'
      ? vioData.gyro_rotation_angle_rad
      : null;
    const gyroRotationSamples = Number(vioData.gyro_rotation_sample_count || 0);
    const gravityMode = vioData.gravity_correction_mode || 'UNKNOWN';
    const gravityAdjustment = vioData.gravity_adjustment_state ||
      (vioData.gravity_correction_applied ? 'ACTIVE' : 'OFF');
    const featureMode = vioData.feature_preprocess_mode || 'UNKNOWN';
    const initValue = initializationData.state || data.auto_initialize_state || 'UNKNOWN';
    const inliers = Number(vioData.visual_inlier_count || 0);
    const matches = Number(vioData.visual_match_count || 0);
    const rawKeypoints = Number(vioData.feature_raw_keypoint_count || 0);
    const normalizedKeypoints = Number(vioData.feature_normalized_keypoint_count || 0);
    const frameLuma = Number(vioData.frame_luma_median || 0);
    const irKeypoints = Number(vioData.ir_keypoint_count || 0);
    const irInliers = Number(vioData.ir_inlier_count || 0);
    const stationary = Number(vioData.gravity_stationary_duration_s || 0);
    const gyroGate = Number(vioData.gravity_gyro_effective_limit_radps || vioData.gravity_gyro_configured_limit_radps || 0.012);
    const gyroNoise = Number(vioData.gravity_gyro_noise_floor_radps || 0);
    const gyroRms = Number(vioData.gravity_gyro_rms_radps || 0);
    const gyroP95 = Number(vioData.gravity_gyro_p95_radps || 0);
    const secondsSinceChunk = typeof cloudData.seconds_since_last_chunk === 'number'
      ? cloudData.seconds_since_last_chunk.toFixed(1) + ' s since last chunk'
      : 'no map chunk yet';
    const cloudTransformAuthority = cloudData.transform_authority || 'UNRESOLVED';
    const cloudCalibrationRevision = cloudData.calibration_revision
      ? ' · calibration ' + String(cloudData.calibration_revision).slice(0, 12)
      : '';
    const visualValue = visualAccepted ? visualSensor + ' UPDATE' : (visualTracking === 'TRACKING' ? 'RECENT VISUAL' : 'VISUAL STALE');
    const visualDetail = inliers + '/' + matches + ' inliers/matches' +
      (visualStale === null ? '' : ' · ' + visualStale.toFixed(2) + ' s stale');
    setStateCard(
      visualState,
      visualValue,
      visualDetail,
      visualAccepted ? 'ok' : stateKind(visualTracking)
    );
    setStateCard(
      poseState,
      poseMode,
      propagationSteps + ' IMU integration steps · accel/gyro history ' +
        accelHistoryCount + '/' + gyroHistoryCount + ' · init window ' +
        initAccelWindowCount + '/' + initGyroWindowCount + ' @ ' +
        (typeof initAccelRateHz === 'number' ? initAccelRateHz.toFixed(1) : 'n/a') + '/' +
        (typeof initGyroRateHz === 'number' ? initGyroRateHz.toFixed(1) : 'n/a') + ' Hz · skew ' +
        (typeof imuTimestampSkewUs === 'number' ? imuTimestampSkewUs + ' µs' : 'n/a'),
      poseMode.includes('IMU_') ? (visualTracking === 'TRACKING' ? 'ok' : 'warn') : stateKind(poseMode)
    );
    const disagreementDegrees = rotationDisagreement === null
      ? 'n/a'
      : (rotationDisagreement * 180 / Math.PI).toFixed(1) + '°';
    const gyroAngleDegrees = gyroRotationAngle === null
      ? 'n/a'
      : (gyroRotationAngle * 180 / Math.PI).toFixed(1) + '°';
    const rotationKind = rotationSource.includes('IMU')
      ? (visualTracking === 'TRACKING' ? 'ok' : 'warn')
      : stateKind(visualTracking);
    setStateCard(
      rotationState,
      rotationSource,
      'visual innovation ' + disagreementDegrees +
        ' · high-rate gyro propagation' +
        (gyroRotationSamples ? ' · ' + gyroRotationSamples + ' samples' : ''),
      rotationKind
    );
    const gravityKind = gravityAdjustment === 'ACTIVE'
      ? 'busy'
      : (gravityAdjustment === 'READY' ? 'ok' : '');
    setStateCard(
      gravityState,
      gravityAdjustment,
      gravityMode + ' · tilt ' + tiltDegrees + '° · still ' + stationary.toFixed(1) +
        ' s · gyro p95 ' + gyroP95.toFixed(4) + '/' + gyroGate.toFixed(4) + ' rad/s',
      gravityKind
    );
    setStateCard(
      featureState,
      featureMode,
      'RGB raw ' + rawKeypoints + ' · normalized ' + normalizedKeypoints +
        ' · IR ' + irInliers + '/' + irKeypoints +
        ' · RGB luma ' + frameLuma.toFixed(0),
      featureMode.includes('CIRCULAR_LCN') ? 'busy' : (featureMode === 'RAW_BASELINE' ? 'ok' : '')
    );
    setStateCard(
      mapState,
      cloudCaptureState,
      Number(cloudData.point_count || 0).toLocaleString() + ' points · ' + secondsSinceChunk +
        ' · transform ' + cloudTransformAuthority + cloudCalibrationRevision +
        ' · ' + (cloudData.capture_reason || 'no capture reason'),
      stateKind(cloudCaptureState)
    );
    setStateCard(
      initState,
      initValue,
      (initializationData.current_subskill || 'idle') + ' · epoch ' + String(epoch).slice(0, 8) +
        ' · ' + initializationBlocker,
      stateKind(initValue)
    );
    const activeWorldFrame = cloudData.world_frame ||
      vioData.world_frame || 'world frame unavailable';
    gravityStatus.textContent =
      'World XYZ · +X front · +Y left · +Z up\n' +
      'frame: ' + activeWorldFrame + '\n' +
      'map transform: ' + cloudTransformAuthority + cloudCalibrationRevision + '\n' +
      '↓ World gravity · -Z\n' +
      'adjustment: ' + gravityAdjustment + ' · ' + gravityMode + '\n' +
      'tilt: ' + tiltDegrees + '° · gyro p95/gate ' + gyroP95.toFixed(4) + '/' + gyroGate.toFixed(4) + '\n' +
      'gyro rms/noise ' + gyroRms.toFixed(4) + '/' + gyroNoise.toFixed(4) + ' rad/s\n' +
      'visual: ' + visualValue + ' · pose: ' + poseMode + '\n' +
      'rotation: ' + rotationSource + ' · innovation ' + disagreementDegrees + '\n' +
      'features: ' + featureMode + ' · IR ' + irInliers + '/' + irKeypoints + ' · map: ' + cloudCaptureState;
    statusBox.textContent = JSON.stringify({
      core: systemCatalog.core || {
        manager: data.manager,
        fabric: data.fabric
      },
      component_counts: {
        providers: catalogProviders.length,
        skills: catalogSkills.length,
        agent_exposed_skills: (data.agent_skill_catalog || []).length
      },
      motion_inhibit: data.motion_inhibit,
      auto_initialize_state: data.auto_initialize_state,
      auto_initialize_error: data.auto_initialize_error
    }, null, 2);
    spaceStatus.textContent = JSON.stringify({
      initialization: data.space_cognition,
      vio: data.vio,
      body_pose: data.body_pose,
      point_cloud: data.world_point_cloud
    }, null, 2);
  } catch (error) {
    statusBox.textContent = String(error);
  }
}

async function requestInitialization(forceReset) {
  if (forceReset && !window.confirm(
    'Establish a new Midbrain spatial origin? This revokes the active ' +
    'workcell calibration, clears observations bound to the old VIO epoch, ' +
    'and requires the robot and camera to remain stationary.'
  )) {
    return;
  }
  initializeButton.disabled = true;
  resetButton.disabled = true;
  spaceStatus.textContent = forceReset ? 'Reinitializing local origin and restarting the map…' : 'Checking startup initialization…';
  actionStatus.textContent = forceReset
    ? 'Reset running: motion inhibited, new VIO epoch requested, map waiting for TRACKING.'
    : 'Initialization verification running.';
  try {
    const response = await fetch('/api/space-cognition/initialize', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({force_reset: forceReset})
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || JSON.stringify(data));
    spaceStatus.textContent = (data.message ? data.message + '\n\n' : '') + JSON.stringify(data, null, 2);
    actionStatus.textContent = data.message || (forceReset ? 'Reset request completed.' : 'Initialization verification completed.');
    await refreshStatus();
  } catch (error) {
    spaceStatus.textContent = 'Error: ' + error;
    actionStatus.textContent = 'Manual action failed: ' + error;
  } finally {
    initializeButton.disabled = false;
    resetButton.disabled = false;
  }
}

initializeButton.addEventListener('click', () => requestInitialization(false));
resetButton.addEventListener('click', () => requestInitialization(true));
clearCloudButton.addEventListener('click', async () => {
  await fetch('/api/world-point-cloud/clear', {method: 'POST'});
});

async function handleDeveloperAgentResult(data) {
  if (data.status !== 'approval_required') {
    answer.textContent = data.answer || 'Completed without a text response.';
    return;
  }
  const approvals = data.approvals || [];
  const summary = approvals.map((approval) => {
    const details = (approval.details || []).map(
      (detail) => detail.label + ': ' + detail.value
    );
    return [
      approval.title || 'Approve protected operation?',
      '',
      approval.summary || 'The Agent requested a protected operation.',
      ...(details.length ? ['', ...details] : []),
      '',
      approval.warning || 'The operation runs only after approval.',
      '',
      'Approve this request?'
    ].join('\n');
  }).join('\n\n');
  answer.textContent = summary;
  const automaticDecision = automaticDeveloperApprovalDecision(approvals);
  const approved = automaticDecision ? true : window.confirm(summary);
  if (automaticDecision) {
    answer.textContent = automaticDecision.label + '\nContinuing the same run...';
  }
  const decisionResponse = await fetch(
    '/api/dev/runs/' + encodeURIComponent(data.run_id) + '/decision',
    {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        approve: approved,
        approval_mode:
          (automaticDecision && automaticDecision.approval_mode) || 'MANUAL',
        max_auto_move_cm:
          (automaticDecision && automaticDecision.max_auto_move_cm) || null,
        max_auto_speed_m_s:
          (automaticDecision && automaticDecision.max_auto_speed_m_s) || null,
        rejection_message: approved ? null : 'Rejected in the developer UI'
      })
    }
  );
  const next = await decisionResponse.json();
  if (!decisionResponse.ok) {
    throw new Error(next.detail || JSON.stringify(next));
  }
  await handleDeveloperAgentResult(next);
}

runButton.addEventListener('click', async () => {
  runButton.disabled = true;
  answer.textContent = 'Running…';
  try {
    const authorization = agentAuthorizationPreferences();
    const response = await fetch('/api/dev/run', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        prompt: promptBox.value,
        agent_model: agentModel.value,
        reasoning_effort: reasoningEffort.value,
        vlm_model: vlmModel.value,
        auto_authorize_provider_activation:
          authorization.autoApproveProviders,
        auto_authorize_relative_motion: authorization.autoApproveMoves,
        max_auto_move_cm: authorization.maxAutoMoveCm,
        max_auto_speed_m_s: authorization.maxAutoSpeedMps,
        auto_authorize_stationary_calibration:
          authorization.autoApproveCalibration,
        auto_authorize_stationary_activation:
          authorization.autoApproveCalibrationActivation
      })
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || JSON.stringify(data));
    await handleDeveloperAgentResult(data);
    refreshStatus();
  } catch (error) {
    answer.textContent = 'Error: ' + error;
  } finally {
    runButton.disabled = false;
  }
});
refreshReplayButton.addEventListener('click', refreshReplayProvenance);

for (const control of [
  autoApproveProviders,
  autoApproveMoves,
  autoApproveCalibration,
  autoApproveCalibrationActivation,
  maxAutoMoveCm,
  maxAutoSpeedMps
]) {
  control.addEventListener('change', updateAgentAuthorizationTicker);
}
maxAutoMoveCm.addEventListener('input', updateAgentAuthorizationTicker);
maxAutoSpeedMps.addEventListener('input', updateAgentAuthorizationTicker);
loadAgentAuthorizationPreferences();

const canvas = document.getElementById('cloud');
const gl = canvas.getContext('webgl', {alpha: false, antialias: true});
let pointCount = 0;
let cloudBuffer = null;
const ISOMETRIC_YAW = -Math.PI / 4;
const ISOMETRIC_PITCH = Math.atan(1 / Math.sqrt(2));
let orbitYaw = ISOMETRIC_YAW;
let orbitPitch = ISOMETRIC_PITCH;
let viewDistance = 12.0;
let orthoScale = 3.5;
let target = [0, 0, 1.5];
let dragging = false;
let dragMode = 'orbit';
let dragX = 0;
let dragY = 0;
let spatialAxisSnapshot = null;
let dynamicAxisFrames = [];
let axisUiSignature = '';
const AXIS_VISIBILITY_KEY = 'midbrain.spatial-axis-visibility.v2';
const axisVisibility = new Map();
try {
  const storedVisibility = JSON.parse(localStorage.getItem(AXIS_VISIBILITY_KEY) || '{}');
  for (const [frameId, visible] of Object.entries(storedVisibility)) {
    axisVisibility.set(frameId, Boolean(visible));
  }
} catch (_error) {
  localStorage.removeItem(AXIS_VISIBILITY_KEY);
}

function shader(type, source) {
  const result = gl.createShader(type);
  gl.shaderSource(result, source);
  gl.compileShader(result);
  if (!gl.getShaderParameter(result, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(result));
  return result;
}

const vertexSource = `
attribute vec3 aPosition;
attribute vec3 aColor;
attribute float aAge;
uniform mat4 uProjection;
uniform mat4 uView;
uniform float uRetention;
uniform float uPointSize;
varying vec4 vColor;
void main() {
  gl_Position = uProjection * uView * vec4(aPosition, 1.0);
  gl_PointSize = uPointSize;
  float alpha = clamp(1.0 - aAge / uRetention, 0.0, 1.0);
  vColor = vec4(aColor, alpha);
}`;
const fragmentSource = `
precision mediump float;
varying vec4 vColor;
void main() {
  vec2 delta = gl_PointCoord - vec2(0.5);
  if (dot(delta, delta) > 0.25) discard;
  gl_FragColor = vColor;
}`;
const lineVertexSource = `
attribute vec3 aPosition;
uniform mat4 uProjection;
uniform mat4 uView;
void main() {
  gl_Position = uProjection * uView * vec4(aPosition, 1.0);
}`;
const lineFragmentSource = `
precision mediump float;
uniform vec4 uColor;
void main() { gl_FragColor = uColor; }`;
const gravityArrowVertices = new Float32Array([
  0.0, 0.0, 0.0,   0.0, 0.0, -1.2,
  0.0, 0.0, -1.2, -0.15, 0.0, -0.95,
  0.0, 0.0, -1.2,  0.15, 0.0, -0.95,
  0.0, 0.0, -1.2,  0.0, -0.15, -0.95,
  0.0, 0.0, -1.2,  0.0, 0.15, -0.95
]);
const localAxisColors = [
  [1.0, 0.35, 0.37, 1.0],
  [0.35, 0.84, 0.55, 1.0],
  [0.30, 0.64, 1.0, 1.0]
];
const cameraMarkerLocal = [
  [0, 0, 0], [-0.18, -0.12, 0.35],
  [0, 0, 0], [ 0.18, -0.12, 0.35],
  [0, 0, 0], [ 0.18,  0.12, 0.35],
  [0, 0, 0], [-0.18,  0.12, 0.35],
  [-0.18, -0.12, 0.35], [ 0.18, -0.12, 0.35],
  [ 0.18, -0.12, 0.35], [ 0.18,  0.12, 0.35],
  [ 0.18,  0.12, 0.35], [-0.18,  0.12, 0.35],
  [-0.18,  0.12, 0.35], [-0.18, -0.12, 0.35],
  [0, 0, 0], [0, 0, 0.55]
];

let program = null;
let lineProgram = null;
let gravityArrowBuffer = null;
let cameraMarkerBuffer = null;
let cameraMarkerVertexCount = 0;
if (gl) {
  program = gl.createProgram();
  gl.attachShader(program, shader(gl.VERTEX_SHADER, vertexSource));
  gl.attachShader(program, shader(gl.FRAGMENT_SHADER, fragmentSource));
  gl.linkProgram(program);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(program));
  cloudBuffer = gl.createBuffer();
  lineProgram = gl.createProgram();
  gl.attachShader(lineProgram, shader(gl.VERTEX_SHADER, lineVertexSource));
  gl.attachShader(lineProgram, shader(gl.FRAGMENT_SHADER, lineFragmentSource));
  gl.linkProgram(lineProgram);
  if (!gl.getProgramParameter(lineProgram, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(lineProgram));
  gravityArrowBuffer = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, gravityArrowBuffer);
  gl.bufferData(gl.ARRAY_BUFFER, gravityArrowVertices, gl.STATIC_DRAW);
  cameraMarkerBuffer = gl.createBuffer();
  gl.enable(gl.DEPTH_TEST);
  gl.enable(gl.BLEND);
  gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
}

function quaternionMatrix(q) {
  if (!Array.isArray(q) || q.length !== 4) return null;
  let [x, y, z, w] = q.map(Number);
  const norm = Math.hypot(x, y, z, w) || 1;
  x /= norm; y /= norm; z /= norm; w /= norm;
  return [
    [1 - 2*(y*y + z*z), 2*(x*y - z*w), 2*(x*z + y*w)],
    [2*(x*y + z*w), 1 - 2*(x*x + z*z), 2*(y*z - x*w)],
    [2*(x*z - y*w), 2*(y*z + x*w), 1 - 2*(x*x + y*y)]
  ];
}

function axisIsVisible(frame) {
  return axisVisibility.has(frame.frame_id)
    ? axisVisibility.get(frame.frame_id)
    : Boolean(frame.default_visible);
}

function saveAxisVisibility() {
  try {
    localStorage.setItem(
      AXIS_VISIBILITY_KEY,
      JSON.stringify(Object.fromEntries(axisVisibility.entries()))
    );
  } catch (_error) {
    return;
  }
}

function rotateDirection(rotation, vector) {
  return [
    rotation[0][0] * vector[0] + rotation[0][1] * vector[1] + rotation[0][2] * vector[2],
    rotation[1][0] * vector[0] + rotation[1][1] * vector[1] + rotation[1][2] * vector[2],
    rotation[2][0] * vector[0] + rotation[2][1] * vector[1] + rotation[2][2] * vector[2]
  ];
}

function axisArrowVertices(origin, direction, length) {
  const unit = normalize(direction);
  const endpoint = origin.map((value, index) => value + unit[index] * length);
  const reference = Math.abs(unit[2]) < 0.82 ? [0, 0, 1] : [0, 1, 0];
  const sideA = normalize(cross(unit, reference));
  const sideB = normalize(cross(unit, sideA));
  const arrowBack = endpoint.map((value, index) => value - unit[index] * length * 0.18);
  const wing = length * 0.065;
  const wingPoints = [sideA, sideA.map(value => -value), sideB, sideB.map(value => -value)].map(
    side => arrowBack.map((value, index) => value + side[index] * wing)
  );
  const values = [...origin, ...endpoint];
  for (const point of wingPoints) values.push(...endpoint, ...point);
  return {vertices: new Float32Array(values), endpoint};
}

const frameLabelNodes = new Map();
function syncFrameLabels(frames) {
  const activeIds = new Set(frames.map(frame => frame.frame_id));
  for (const [frameId, node] of frameLabelNodes.entries()) {
    if (!activeIds.has(frameId)) {
      node.remove();
      frameLabelNodes.delete(frameId);
    }
  }
  for (const frame of frames) {
    let node = frameLabelNodes.get(frame.frame_id);
    if (!node) {
      node = document.createElement('span');
      node.className = 'frame-label';
      frameLabels.append(node);
      frameLabelNodes.set(frame.frame_id, node);
    }
    node.textContent = frame.short_label || frame.frame_id;
  }
}

function rebuildDynamicAxisBuffers(frames) {
  if (!gl) return;
  for (const frame of dynamicAxisFrames) {
    for (const axis of frame.axes) gl.deleteBuffer(axis.buffer);
  }
  dynamicAxisFrames = [];
  for (const frame of frames) {
    if (!frame.available) continue;
    const origin = Array.isArray(frame.translation_m) ? frame.translation_m.map(Number) : null;
    const rotation = quaternionMatrix(frame.rotation_xyzw);
    if (!origin || origin.length !== 3 || !rotation || origin.some(value => !Number.isFinite(value))) continue;
    const length = Math.max(0.04, Number(frame.axis_length_m) || 0.16);
    const localDirections = [[1, 0, 0], [0, 1, 0], [0, 0, 1]];
    const axes = localDirections.map(localDirection => {
      const arrow = axisArrowVertices(origin, rotateDirection(rotation, localDirection), length);
      const buffer = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
      gl.bufferData(gl.ARRAY_BUFFER, arrow.vertices, gl.DYNAMIC_DRAW);
      return {buffer, vertexCount: arrow.vertices.length / 3, endpoint: arrow.endpoint};
    });
    dynamicAxisFrames.push({...frame, origin, axes});
  }
  syncFrameLabels(dynamicAxisFrames);
}

const axisMetadataNodes = new Map();
function frameMetadata(frame) {
  if (frame.frame_role === 'SCREEN_2D') return frame;
  return {
    frame_id: frame.frame_id,
    frame_role: frame.frame_role,
    convention_id: frame.convention_id,
    available: frame.available,
    world_translation_m: frame.translation_m || null,
    world_rotation_xyzw: frame.rotation_xyzw || null,
    transform_at_us: frame.at_us || null,
    transform_path: frame.path || [],
    error: frame.error || null
  };
}

function updateAxisMetadata(frames) {
  for (const frame of frames) {
    const node = axisMetadataNodes.get(frame.frame_id);
    if (node) node.textContent = JSON.stringify(frameMetadata(frame), null, 2);
  }
}

function renderAxisControls(snapshot) {
  const frames = [...(snapshot.frames || [])];
  if (snapshot.screen_space) frames.push(snapshot.screen_space);
  const roleOrder = {
    WORLD: 0, ARM_BASE: 1, GRIPPER_TOOL: 2, CAMERA_LEVEL: 3,
    CAMERA_OPTICAL: 4, ARM_JOINT: 5, OBJECT: 6, BODY: 7,
    SENSOR: 8, SCREEN_2D: 9, OTHER: 10
  };
  frames.sort((left, right) =>
    (roleOrder[left.frame_role] ?? 99) - (roleOrder[right.frame_role] ?? 99) ||
    String(left.short_label || left.frame_id).localeCompare(String(right.short_label || right.frame_id))
  );
  const signature = frames.map(
    frame => [frame.frame_id, frame.frame_role, Boolean(frame.available)].join(':')
  ).join('|');
  if (signature !== axisUiSignature) {
    axisUiSignature = signature;
    axisMetadataNodes.clear();
    axisControls.replaceChildren();
    for (const frame of frames) {
      const item = document.createElement('div');
      item.className = 'axis-item' + (frame.available ? '' : ' unavailable');
      const row = document.createElement('label');
      row.className = 'axis-row';
      const checkbox = document.createElement('input');
      checkbox.type = 'checkbox';
      checkbox.checked = axisIsVisible(frame);
      checkbox.disabled = !frame.available;
      checkbox.dataset.frameId = frame.frame_id;
      checkbox.addEventListener('change', () => {
        axisVisibility.set(frame.frame_id, checkbox.checked);
        saveAxisVisibility();
        screenAxisOverlay.hidden = !axisIsVisible(snapshot.screen_space || {});
      });
      const name = document.createElement('span');
      name.className = 'axis-name';
      name.textContent = frame.short_label || frame.frame_id;
      name.title = frame.frame_id;
      const role = document.createElement('span');
      role.className = 'axis-role';
      role.textContent = frame.available ? frame.frame_role : frame.frame_role + ' · unavailable';
      row.append(checkbox, name, role);
      const details = document.createElement('details');
      const summary = document.createElement('summary');
      summary.textContent = 'metadata';
      const metadata = document.createElement('pre');
      axisMetadataNodes.set(frame.frame_id, metadata);
      details.append(summary, metadata);
      item.append(row, details);
      axisControls.append(item);
    }
  }
  updateAxisMetadata(frames);
  screenAxisOverlay.hidden = !snapshot.screen_space || !axisIsVisible(snapshot.screen_space);
  const liveCount = frames.filter(frame => frame.available && frame.frame_role !== 'SCREEN_2D').length;
  axisStatus.textContent = liveCount + ' live 3D frames · epoch ' + String(snapshot.session_epoch || 'unknown').slice(0, 12);
}

async function refreshSpatialAxes() {
  try {
    const response = await fetch('/api/spatial/axes?t=' + Date.now(), {cache: 'no-store'});
    if (!response.ok) throw new Error(await response.text());
    spatialAxisSnapshot = await response.json();
    const frames = Array.isArray(spatialAxisSnapshot.frames) ? spatialAxisSnapshot.frames : [];
    rebuildDynamicAxisBuffers(frames);
    renderAxisControls(spatialAxisSnapshot);
  } catch (error) {
    axisStatus.textContent = 'Spatial axes unavailable: ' + error;
  }
}

function updateCameraMarker(pose) {
  if (!gl || !cameraMarkerBuffer || !pose) {
    cameraMarkerVertexCount = 0;
    return;
  }
  const rotation = quaternionMatrix(pose.rotation_xyzw);
  const translation = Array.isArray(pose.translation_m) ? pose.translation_m.map(Number) : null;
  if (!rotation || !translation || translation.length !== 3) {
    cameraMarkerVertexCount = 0;
    return;
  }
  const values = [];
  for (const point of cameraMarkerLocal) {
    values.push(
      translation[0] + rotation[0][0]*point[0] + rotation[0][1]*point[1] + rotation[0][2]*point[2],
      translation[1] + rotation[1][0]*point[0] + rotation[1][1]*point[1] + rotation[1][2]*point[2],
      translation[2] + rotation[2][0]*point[0] + rotation[2][1]*point[1] + rotation[2][2]*point[2]
    );
  }
  cameraMarkerVertexCount = values.length / 3;
  gl.bindBuffer(gl.ARRAY_BUFFER, cameraMarkerBuffer);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(values), gl.DYNAMIC_DRAW);
}

function orthographic(left, right, bottom, top, near, far) {
  const lr = 1 / (left - right);
  const bt = 1 / (bottom - top);
  const nf = 1 / (near - far);
  return new Float32Array([
    -2 * lr, 0, 0, 0,
    0, -2 * bt, 0, 0,
    0, 0, 2 * nf, 0,
    (left + right) * lr, (top + bottom) * bt, (far + near) * nf, 1
  ]);
}

function normalize(v) {
  const length = Math.hypot(v[0], v[1], v[2]) || 1;
  return [v[0] / length, v[1] / length, v[2] / length];
}
function cross(a, b) {
  return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]];
}
function dot(a, b) { return a[0]*b[0] + a[1]*b[1] + a[2]*b[2]; }
function lookAt(eye, center, up) {
  const z = normalize([eye[0]-center[0], eye[1]-center[1], eye[2]-center[2]]);
  const x = normalize(cross(up, z));
  const y = cross(z, x);
  return new Float32Array([
    x[0], y[0], z[0], 0,
    x[1], y[1], z[1], 0,
    x[2], y[2], z[2], 0,
    -dot(x, eye), -dot(y, eye), -dot(z, eye), 1
  ]);
}

function transformMat4(matrix, vector) {
  return [
    matrix[0] * vector[0] + matrix[4] * vector[1] + matrix[8] * vector[2] + matrix[12] * vector[3],
    matrix[1] * vector[0] + matrix[5] * vector[1] + matrix[9] * vector[2] + matrix[13] * vector[3],
    matrix[2] * vector[0] + matrix[6] * vector[1] + matrix[10] * vector[2] + matrix[14] * vector[3],
    matrix[3] * vector[0] + matrix[7] * vector[1] + matrix[11] * vector[2] + matrix[15] * vector[3]
  ];
}

function updateFrameLabelPositions(projection, view) {
  for (const frame of dynamicAxisFrames) {
    const node = frameLabelNodes.get(frame.frame_id);
    if (!node) continue;
    if (!axisIsVisible(frame)) {
      node.hidden = true;
      continue;
    }
    const cameraPoint = transformMat4(view, [...frame.origin, 1]);
    const clip = transformMat4(projection, cameraPoint);
    const w = clip[3] || 1;
    const ndc = [clip[0] / w, clip[1] / w, clip[2] / w];
    const outside = ndc[0] < -1.05 || ndc[0] > 1.05 || ndc[1] < -1.05 || ndc[1] > 1.05 || ndc[2] < -1 || ndc[2] > 1;
    node.hidden = outside;
    if (outside) continue;
    node.style.left = ((ndc[0] * 0.5 + 0.5) * canvas.clientWidth).toFixed(1) + 'px';
    node.style.top = ((1 - (ndc[1] * 0.5 + 0.5)) * canvas.clientHeight).toFixed(1) + 'px';
  }
}

function resizeCanvas() {
  if (!gl) return;
  const ratio = window.devicePixelRatio || 1;
  const width = Math.max(1, Math.floor(canvas.clientWidth * ratio));
  const height = Math.max(1, Math.floor(canvas.clientHeight * ratio));
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
  }
  gl.viewport(0, 0, width, height);
}

function renderCloud() {
  if (!gl || !program || !lineProgram) return;
  resizeCanvas();
  gl.clearColor(0.018, 0.018, 0.018, 1.0);
  gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
  const cp = Math.cos(orbitPitch);
  const eye = [
    target[0] + viewDistance * cp * Math.cos(orbitYaw),
    target[1] + viewDistance * cp * Math.sin(orbitYaw),
    target[2] + viewDistance * Math.sin(orbitPitch)
  ];
  const aspect = canvas.width / Math.max(1, canvas.height);
  const halfHeight = orthoScale;
  const halfWidth = halfHeight * aspect;
  const projection = orthographic(-halfWidth, halfWidth, -halfHeight, halfHeight, 0.03, 100.0);
  const view = lookAt(eye, target, [0, 0, 1]);
  if (pointCount > 0) {
    gl.useProgram(program);
    gl.uniformMatrix4fv(gl.getUniformLocation(program, 'uProjection'), false, projection);
    gl.uniformMatrix4fv(gl.getUniformLocation(program, 'uView'), false, view);
    gl.uniform1f(gl.getUniformLocation(program, 'uRetention'), 10.0);
    gl.uniform1f(
      gl.getUniformLocation(program, 'uPointSize'),
      Math.min(5.0, 2.2 * (window.devicePixelRatio || 1))
    );
    gl.bindBuffer(gl.ARRAY_BUFFER, cloudBuffer);
    const stride = 7 * 4;
    const position = gl.getAttribLocation(program, 'aPosition');
    const color = gl.getAttribLocation(program, 'aColor');
    const age = gl.getAttribLocation(program, 'aAge');
    gl.enableVertexAttribArray(position);
    gl.enableVertexAttribArray(color);
    gl.enableVertexAttribArray(age);
    gl.vertexAttribPointer(position, 3, gl.FLOAT, false, stride, 0);
    gl.vertexAttribPointer(color, 3, gl.FLOAT, false, stride, 3 * 4);
    gl.vertexAttribPointer(age, 1, gl.FLOAT, false, stride, 6 * 4);
    gl.drawArrays(gl.POINTS, 0, pointCount);
  }
  gl.disable(gl.DEPTH_TEST);
  gl.useProgram(lineProgram);
  gl.uniformMatrix4fv(gl.getUniformLocation(lineProgram, 'uProjection'), false, projection);
  gl.uniformMatrix4fv(gl.getUniformLocation(lineProgram, 'uView'), false, view);
  const linePosition = gl.getAttribLocation(lineProgram, 'aPosition');
  gl.enableVertexAttribArray(linePosition);
  for (const frame of dynamicAxisFrames) {
    if (!axisIsVisible(frame)) continue;
    for (let index = 0; index < frame.axes.length; index += 1) {
      gl.uniform4fv(
        gl.getUniformLocation(lineProgram, 'uColor'),
        localAxisColors[index]
      );
      gl.bindBuffer(gl.ARRAY_BUFFER, frame.axes[index].buffer);
      gl.vertexAttribPointer(linePosition, 3, gl.FLOAT, false, 0, 0);
      gl.drawArrays(gl.LINES, 0, frame.axes[index].vertexCount);
    }
  }
  gl.uniform4f(gl.getUniformLocation(lineProgram, 'uColor'), 1.0, 0.38, 0.18, 1.0);
  gl.bindBuffer(gl.ARRAY_BUFFER, gravityArrowBuffer);
  gl.vertexAttribPointer(linePosition, 3, gl.FLOAT, false, 0, 0);
  gl.drawArrays(gl.LINES, 0, gravityArrowVertices.length / 3);
  if (cameraMarkerVertexCount > 0) {
    gl.uniform4f(gl.getUniformLocation(lineProgram, 'uColor'), 0.20, 0.85, 1.0, 1.0);
    gl.bindBuffer(gl.ARRAY_BUFFER, cameraMarkerBuffer);
    gl.vertexAttribPointer(linePosition, 3, gl.FLOAT, false, 0, 0);
    gl.drawArrays(gl.LINES, 0, cameraMarkerVertexCount);
  }
  gl.enable(gl.DEPTH_TEST);
  updateFrameLabelPositions(projection, view);
  requestAnimationFrame(renderCloud);
}

async function refreshCloud() {
  if (!gl) {
    cloudStats.textContent = 'WebGL is unavailable in this browser.';
    return;
  }
  try {
    const response = await fetch('/api/world-point-cloud?t=' + Date.now(), {cache: 'no-store'});
    if (!response.ok) throw new Error(await response.text());
    const buffer = await response.arrayBuffer();
    if (buffer.byteLength < 4) return;
    const view = new DataView(buffer);
    pointCount = view.getUint32(0, true);
    const expectedBytes = 4 + pointCount * 7 * 4;
    if (buffer.byteLength < expectedBytes) throw new Error('truncated point-cloud payload');
    gl.bindBuffer(gl.ARRAY_BUFFER, cloudBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Uint8Array(buffer, 4, pointCount * 28), gl.DYNAMIC_DRAW);
    cloudStats.textContent = pointCount.toLocaleString() + ' points · ' + cloudCaptureState + ' · 10 s retention';
  } catch (error) {
    cloudStats.textContent = 'Point cloud: ' + error + ' · ' + cloudCaptureState;
  }
}

function fitVisibleAxes() {
  const points = [];
  for (const frame of dynamicAxisFrames) {
    if (!axisIsVisible(frame)) continue;
    points.push(frame.origin, ...frame.axes.map(axis => axis.endpoint));
  }
  if (!points.length) return;
  const minimum = [...points[0]];
  const maximum = [...points[0]];
  for (const point of points.slice(1)) {
    for (let index = 0; index < 3; index += 1) {
      minimum[index] = Math.min(minimum[index], point[index]);
      maximum[index] = Math.max(maximum[index], point[index]);
    }
  }
  target = minimum.map((value, index) => (value + maximum[index]) * 0.5);
  const span = Math.max(...maximum.map((value, index) => value - minimum[index]));
  orthoScale = Math.max(0.25, Math.min(30, span * 0.72 + 0.16));
}

canvas.addEventListener('contextmenu', event => event.preventDefault());
canvas.addEventListener('pointerdown', event => {
  event.preventDefault();
  dragging = true;
  dragMode = event.shiftKey || event.button === 1 || event.button === 2 ? 'pan' : 'orbit';
  dragX = event.clientX;
  dragY = event.clientY;
  canvas.setPointerCapture(event.pointerId);
});
canvas.addEventListener('pointermove', event => {
  if (!dragging) return;
  const deltaX = event.clientX - dragX;
  const deltaY = event.clientY - dragY;
  if (dragMode === 'pan') {
    const cp = Math.cos(orbitPitch);
    const viewDirection = [cp * Math.cos(orbitYaw), cp * Math.sin(orbitYaw), Math.sin(orbitPitch)];
    const viewRight = normalize([-Math.sin(orbitYaw), Math.cos(orbitYaw), 0]);
    const viewUp = normalize(cross(viewDirection, viewRight));
    const worldPerPixel = 2 * orthoScale / Math.max(1, canvas.clientHeight);
    target = target.map(
      (value, index) => value - deltaX * viewRight[index] * worldPerPixel + deltaY * viewUp[index] * worldPerPixel
    );
  } else {
    orbitYaw -= deltaX * 0.006;
    orbitPitch = Math.max(-1.45, Math.min(1.45, orbitPitch + deltaY * 0.006));
  }
  dragX = event.clientX; dragY = event.clientY;
});
function stopCanvasDrag(event) {
  dragging = false;
  if (canvas.hasPointerCapture(event.pointerId)) canvas.releasePointerCapture(event.pointerId);
}
canvas.addEventListener('pointerup', stopCanvasDrag);
canvas.addEventListener('pointercancel', stopCanvasDrag);
canvas.addEventListener('wheel', event => {
  event.preventDefault();
  orthoScale = Math.max(0.25, Math.min(30, orthoScale * Math.exp(event.deltaY * 0.001)));
}, {passive: false});
resetViewButton.addEventListener('click', () => {
  orbitYaw = ISOMETRIC_YAW;
  orbitPitch = ISOMETRIC_PITCH;
  orthoScale = 3.5;
  target = [0, 0, 1.5];
});
fitAxesButton.addEventListener('click', fitVisibleAxes);

refreshStatus();
if (!focusedUtilityPage) refreshReplayProvenance();
renderCloud();
refreshCloud();
refreshSpatialAxes();
setInterval(refreshStatus, 2500);
if (!focusedUtilityPage) {
  setInterval(refreshReplayProvenance, 10000);
  setInterval(refreshAuthorization, 1000);
  refreshAuthorization();
}
setInterval(refreshCloud, 300);
setInterval(refreshSpatialAxes, 1500);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
