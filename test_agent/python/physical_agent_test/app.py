from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from agents import RunState, SQLiteSession
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from .agent_driver import PrototypeAgentDriver
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
from .stationary_calibration_adapter import StationaryCalibrationSkillAdapter
from .tool_registration_adapter import ToolControlFrameSkillAdapter
from .world_point_cloud import WorldPointCloudAccumulator
from .vlm_router import build_default_vlm_router
from stationary_world_arm_alignment.camera import RgbdCapture
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
integrated_motion_agent_adapter = IntegratedRelativeMotionAdapter(integrated)
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
stationary_calibration_agent_adapter = StationaryCalibrationSkillAdapter(
    AlignmentSkill,
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
        "midbrain-regular-agent-systemic-gui-v2",
        agent_session_database,
    ),
    defer_loading=settings.agent_skill_defer_loading,
    adapter_timeout_s=phase4_policy.skill_adapter_timeout_s,
    max_turns=settings.openai_agent_max_turns,
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
        "midbrain-developer-agent-systemic-gui-v2",
        agent_session_database,
    ),
    defer_loading=settings.agent_skill_defer_loading,
    adapter_timeout_s=phase4_policy.skill_adapter_timeout_s,
    max_turns=settings.openai_agent_max_turns,
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


app = FastAPI(title="Physical Agent Test Scaffold", version="0.3.0", lifespan=lifespan)


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


@app.post("/api/run")
async def run_prompt(request: PromptRequest) -> dict[str, Any]:
    run_id = str(uuid.uuid4())
    agent_model, reasoning_effort, vlm_model = _model_selection(request)
    try:
        return await _regular_agent_step(
            request.prompt,
            run_id,
            agent_model=agent_model,
            reasoning_effort=reasoning_effort,
            vlm_model=vlm_model,
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
) -> dict[str, Any]:
    result = await operation_registry.run(
        f"openai_regular_agent_run:{run_id}",
        driver.run_interactive(
            input_value,
            model_override=agent_model,
            reasoning_effort=reasoning_effort,
            vlm_model_override=vlm_model,
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
    async with pending_regular_runs_lock:
        pending_regular_runs[run_id] = PendingAgentRun(
            state=result.state,
            created_monotonic=time.monotonic(),
            agent_model=agent_model,
            reasoning_effort=reasoning_effort,
            vlm_model=vlm_model,
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
) -> dict[str, Any]:
    result = await operation_registry.run(
        f"openai_developer_agent_run:{run_id}",
        developer_driver.run_interactive(
            input_value,
            model_override=agent_model,
            reasoning_effort=reasoning_effort,
            vlm_model_override=vlm_model,
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
    async with pending_developer_runs_lock:
        pending_developer_runs[run_id] = PendingAgentRun(
            state=result.state,
            created_monotonic=time.monotonic(),
            agent_model=agent_model,
            reasoning_effort=reasoning_effort,
            vlm_model=vlm_model,
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
    try:
        return await _developer_agent_step(
            request.prompt,
            run_id,
            agent_model=agent_model,
            reasoning_effort=reasoning_effort,
            vlm_model=vlm_model,
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
        <div class="state-card" id="bindingState"><div class="state-label">Camera capability binding</div><div class="state-value"><span class="state-lamp"></span><span class="state-text">NOT REQUESTED</span></div><div class="state-detail">Manager binding has not been requested</div></div>
        <div class="model-controls">
          <label>Agent model<select id="agentModel"><option value="gpt-5.6-terra">GPT-5.6 Terra</option></select></label>
          <label>Reasoning<select id="reasoningEffort"><option value="medium">Medium</option></select></label>
          <label>Visual model<select id="vlmModel"><option value="auto">Auto routing</option></select></label>
        </div>
        <textarea id="prompt">Take a screenshot and identify the object I am pointing at. Use only the RGB image.</textarea>
        <div>
          <button class="primary" id="run">Run prompt</button>
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
        <button class="secondary" id="resetView">Reset isometric view</button>
        <div class="viewer-wrap">
          <canvas id="cloud"></canvas>
          <div class="viewer-overlay" id="cloudStats">Waiting for pose and RGB-D…</div>
          <div class="gravity-overlay" id="gravityStatus">↓ World gravity · -Y</div>
        </div>
        <p class="controls">Orthographic isometric view. Drag to orbit, mouse wheel to change parallel scale. Orange is world-down; cyan is the current camera pose. Points use world coordinates and fade linearly over 10 seconds.</p>
      </section>
      <section class="card" id="spaceCognitionLinkPanel">
        <div class="role-kicker">Point-cloud recovery</div>
        <h2>Space cognition</h2>
        <p class="sub">If the world point cloud stops updating or the local frame has drifted, inspect Space Cognition before deliberately resetting its origin.</p>
        <a href="/dev/skills/initialize-space-cognition" style="color:var(--mb-warning)">Open Space Cognition development UI →</a>
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
const bindingState = document.getElementById('bindingState');
const featureState = document.getElementById('featureState');
const mapState = document.getElementById('mapState');
const initState = document.getElementById('initState');
const actionStatus = document.getElementById('actionStatus');
let cloudCaptureState = 'unknown';
let latestCameraPose = null;
let activeAuthorizationId = null;

async function refreshAuthorization() {
  if (dedicatedSpaceCognition) return;
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
  if (dedicatedSpaceCognition) return;
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
    const binding = data.capability_binding || {};
    const bindingValidity = binding.validity || binding.status || 'NOT_REQUESTED';
    const bindingIssues = Array.isArray(binding.validation_issues)
      ? binding.validation_issues.join('; ')
      : (binding.reason || 'no validation issue');
    const bindingKind = bindingValidity === 'CURRENT'
      ? 'ok'
      : (bindingValidity.includes('FALLBACK') ? 'warn' :
        (bindingValidity.includes('STALE') || bindingValidity === 'UNRESOLVED' ? 'bad' : ''));
    setStateCard(
      bindingState,
      bindingValidity,
      (binding.binding_id ? 'binding ' + String(binding.binding_id).slice(0, 8) + ' · ' : '') +
        bindingIssues,
      bindingKind
    );
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
    gravityStatus.textContent = '↓ World gravity · -Y\n' +
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
  const approved = window.confirm(summary);
  const decisionResponse = await fetch(
    '/api/dev/runs/' + encodeURIComponent(data.run_id) + '/decision',
    {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        approve: approved,
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
    const response = await fetch('/api/dev/run', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        prompt: promptBox.value,
        agent_model: agentModel.value,
        reasoning_effort: reasoningEffort.value,
        vlm_model: vlmModel.value
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
let dragX = 0;
let dragY = 0;

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
  0.0, 0.0, 0.0,   0.0, -1.2, 0.0,
  0.0, -1.2, 0.0, -0.15, -0.95, 0.0,
  0.0, -1.2, 0.0,  0.15, -0.95, 0.0,
  0.0, -1.2, 0.0,  0.0, -0.95, -0.15,
  0.0, -1.2, 0.0,  0.0, -0.95, 0.15
]);
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
    target[0] + viewDistance * cp * Math.sin(orbitYaw),
    target[1] + viewDistance * Math.sin(orbitPitch),
    target[2] + viewDistance * cp * Math.cos(orbitYaw)
  ];
  const aspect = canvas.width / Math.max(1, canvas.height);
  const halfHeight = orthoScale;
  const halfWidth = halfHeight * aspect;
  const projection = orthographic(-halfWidth, halfWidth, -halfHeight, halfHeight, 0.03, 100.0);
  const view = lookAt(eye, target, [0, 1, 0]);
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
  gl.uniform4f(gl.getUniformLocation(lineProgram, 'uColor'), 1.0, 0.38, 0.18, 1.0);
  gl.bindBuffer(gl.ARRAY_BUFFER, gravityArrowBuffer);
  const linePosition = gl.getAttribLocation(lineProgram, 'aPosition');
  gl.enableVertexAttribArray(linePosition);
  gl.vertexAttribPointer(linePosition, 3, gl.FLOAT, false, 0, 0);
  gl.drawArrays(gl.LINES, 0, gravityArrowVertices.length / 3);
  if (cameraMarkerVertexCount > 0) {
    gl.uniform4f(gl.getUniformLocation(lineProgram, 'uColor'), 0.20, 0.85, 1.0, 1.0);
    gl.bindBuffer(gl.ARRAY_BUFFER, cameraMarkerBuffer);
    gl.vertexAttribPointer(linePosition, 3, gl.FLOAT, false, 0, 0);
    gl.drawArrays(gl.LINES, 0, cameraMarkerVertexCount);
  }
  gl.enable(gl.DEPTH_TEST);
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

canvas.addEventListener('pointerdown', event => {
  dragging = true; dragX = event.clientX; dragY = event.clientY; canvas.setPointerCapture(event.pointerId);
});
canvas.addEventListener('pointermove', event => {
  if (!dragging) return;
  orbitYaw -= (event.clientX - dragX) * 0.006;
  orbitPitch = Math.max(-1.45, Math.min(1.45, orbitPitch + (event.clientY - dragY) * 0.006));
  dragX = event.clientX; dragY = event.clientY;
});
canvas.addEventListener('pointerup', event => { dragging = false; canvas.releasePointerCapture(event.pointerId); });
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

refreshStatus();
if (!dedicatedSpaceCognition) refreshReplayProvenance();
renderCloud();
refreshCloud();
setInterval(refreshStatus, 2500);
if (!dedicatedSpaceCognition) {
  setInterval(refreshReplayProvenance, 10000);
  setInterval(refreshAuthorization, 1000);
  refreshAuthorization();
}
setInterval(refreshCloud, 300);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
