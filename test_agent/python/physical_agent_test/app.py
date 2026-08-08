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
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from .agent_attachments import (
    AgentAttachmentStore,
    build_multimodal_user_input,
)
from .agent_driver import (
    AgentEventSink,
    AgentInput,
    AgentSessionAuthorization,
    PERFORM_RELATIVE_EFFECTOR_MOTION_TOOL,
    PrototypeAgentDriver,
    relative_motion_within_authorization,
)
from .agent_event_stream import (
    AgentRunStreamRegistry,
    parse_event_sequence,
    stream_sse,
)
from .agent_run_journal import AgentRunJournal
from .authorization import AuthorizationStore
from .basic_client import BasicControllerClient
from .basic_safe_home_adapter import BasicSafeHomeAdapter
from .config import Settings
from .depth_capture import DepthCapture
from .effector_front_adapter import EffectorFrontSkillAdapter
from .external_skill_host import (
    ExternalSkillHostServices,
    load_external_skill_host_adapters,
)
from .fabric_client import FabricClient
from .gemini_pointing_skill import (
    PointingIdentificationSkill,
    VisualSceneAnalysisSkill,
)
from .initialize_space_cognition_skill import InitializeSpaceCognitionSkill
from .integrated_client import IntegratedControllerClient
from .integrated_motion_adapter import IntegratedRelativeMotionAdapter
from .item_locator_adapter import MetricItemLocatorAdapter
from .manager_client import ManagerClient
from .no_contact_approach import NoContactItemApproachAdapter
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
from .visual_evidence import VisualEvidenceStore
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
agent_run_journal = AgentRunJournal(
    settings.agent_run_journal_path,
    maximum_runs=settings.agent_run_journal_max_runs,
    maximum_events_per_run=(
        settings.agent_run_journal_max_events_per_run
    ),
    retention_days=settings.agent_run_journal_retention_days,
)
agent_run_stream_registry = AgentRunStreamRegistry(
    maximum_events=settings.agent_run_journal_max_events_per_run,
    journal=agent_run_journal,
)
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
capture = RgbCapture(
    fabric,
    settings.screenshot_dir,
    first_frame_timeout_s=settings.camera_first_frame_timeout_s,
)
depth_capture = DepthCapture(fabric, settings.screenshot_dir)
visual_evidence_store = VisualEvidenceStore()
agent_attachment_store = AgentAttachmentStore()
agent_vlm_router = build_default_vlm_router(
    gemini_model=settings.gemini_model,
    attempt_timeout_s=phase4_policy.vlm_attempt_timeout_s,
    attempts_per_backend=phase4_policy.vlm_attempts_per_backend,
    retry_backoff_s=phase4_policy.vlm_retry_backoff_s,
)
pointing_skill = PointingIdentificationSkill(
    capture,
    settings.gemini_model,
    manager=manager,
    fallback_camera_provider_id=settings.head_camera_provider_id,
    vlm_router=agent_vlm_router,
    visual_evidence_store=visual_evidence_store,
    capture_attempts=settings.camera_skill_capture_attempts,
    capture_retry_backoff_s=settings.camera_skill_retry_backoff_s,
)
visual_scene_skill = VisualSceneAnalysisSkill(
    capture,
    settings.gemini_model,
    manager=manager,
    fallback_camera_provider_id=settings.head_camera_provider_id,
    vlm_router=agent_vlm_router,
    visual_evidence_store=visual_evidence_store,
    capture_attempts=settings.camera_skill_capture_attempts,
    capture_retry_backoff_s=settings.camera_skill_retry_backoff_s,
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
from .semantic_scene_inspector import SemanticSceneInspector
from .semantic_assertion_publisher import SemanticAssertionPublisher
from .scene_segmentation_policy_publisher import (
    SceneSegmentationPolicyPublisher,
)


async def _ensure_current_world_tracking() -> dict[str, Any]:
    return await space_cognition_skill.ensure_tracking()


spatial_registration_skill = SpatialRegistrationSkillAdapter(
    RgbdCapture(fabric, settings.head_camera_frame),
    fabric,
    manager=manager,
    fallback_camera_provider_id=settings.head_camera_provider_id,
    binding_mode=settings.phase5_spatial_binding_mode,
    generic_route_mode=settings.phase5_spatial_generic_route_mode,
    mounted_static_target_frames={settings.arm_base_frame},
    readiness_ensurer=_ensure_current_world_tracking,
)
agent_skill_catalog = discover_agent_skills(
    settings.workspace_root,
    include_disabled=True,
)
external_skill_adapters = load_external_skill_host_adapters(
    agent_skill_catalog,
    eligible_tool_names=set(settings.phase4_eligible_tools),
    services=ExternalSkillHostServices(
        manager=manager,
        fabric=fabric,
        spatial=spatial_registration_skill,
        vlm_router=agent_vlm_router,
        visual_evidence_store=visual_evidence_store,
    ),
)
semantic_assertion_publisher = SemanticAssertionPublisher(fabric)
scene_segmentation_policy_publisher = SceneSegmentationPolicyPublisher(
    fabric,
    state_path=settings.scene_policy_state_path,
)
semantic_scene_inspector = SemanticSceneInspector(
    fabric,
    tracker_base_url=settings.sam2_scene_tracker_url,
    visual_evidence_store=visual_evidence_store,
)
item_locator_skill = MetricItemLocatorAdapter(
    spatial_registration_skill,
    agent_vlm_router,
    evidence_dir=settings.screenshot_dir,
    semantic_assertion_publisher=semantic_assertion_publisher,
    visual_evidence_store=visual_evidence_store,
)
effector_front_skill = EffectorFrontSkillAdapter(
    spatial_registration_skill,
    agent_vlm_router,
    evidence_dir=settings.screenshot_dir,
    arm_tool_frame=settings.arm_tool_frame,
    visual_evidence_store=visual_evidence_store,
)
no_contact_approach_skill = NoContactItemApproachAdapter(
    item_locator_skill,
    effector_front_skill,
    scene_inspector=semantic_scene_inspector,
    manager=manager,
    integrated=integrated,
    authorization_store=authorization_store,
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


agent_session_database = (
    settings.workspace_root
    / "test_agent"
    / "run"
    / "agent_sessions.sqlite3"
)
agent_session_database.parent.mkdir(parents=True, exist_ok=True)
agent_runtime_session_epoch = uuid.uuid4().hex
midbrain_session_id = f"agent-process-{agent_runtime_session_epoch}"
midbrain_session_source = "agent_process_fallback"
midbrain_session_identity_lock = asyncio.Lock()
midbrain_session_identity_checked_at = 0.0


async def _refresh_midbrain_session_identity() -> None:
    """Track Manager restarts without coupling a run to a browser tab."""

    global midbrain_session_id, midbrain_session_source
    global midbrain_session_identity_checked_at
    now = time.monotonic()
    if now - midbrain_session_identity_checked_at < 2.0:
        return
    async with midbrain_session_identity_lock:
        now = time.monotonic()
        if now - midbrain_session_identity_checked_at < 2.0:
            return
        midbrain_session_identity_checked_at = now
        try:
            manager_identity = await asyncio.wait_for(
                manager.health(),
                timeout=2.0,
            )
        except Exception:
            return
        manager_boot_id = str(manager_identity.get("boot_id") or "").strip()
        if not manager_boot_id or manager_boot_id == midbrain_session_id:
            return
        midbrain_session_id = manager_boot_id
        midbrain_session_source = "manager_boot_id"
        await agent_run_journal.set_active_session(manager_boot_id)


def _build_autonomous_agent_driver() -> PrototypeAgentDriver:
    """Build the single autonomous Agent used by every chat surface."""

    return PrototypeAgentDriver(
        pointing_skill,
        settings.openai_model,
        tool_choice=settings.openai_agent_tool_choice,
        eligible_tool_names=set(settings.phase4_eligible_tools),
        visual_scene_skill=visual_scene_skill,
        rgbd_alignment_skill=rgbd_alignment_skill,
        spatial_registration_skill=spatial_registration_skill,
        item_locator_skill=item_locator_skill,
        effector_front_skill=effector_front_skill,
        no_contact_approach_skill=no_contact_approach_skill,
        semantic_scene_inspector=semantic_scene_inspector,
        scene_policy_publisher=scene_segmentation_policy_publisher,
        tool_registration_skill=tool_registration_skill,
        external_skill_adapters=external_skill_adapters,
        stationary_calibration_skill=stationary_calibration_agent_adapter,
        manager=manager,
        provider_lifecycle_control=True,
        integrated_motion_skill=integrated_motion_agent_adapter,
        basic_safe_home_skill=basic_safe_home_agent_adapter,
        space_cognition_establisher=_ensure_current_world_tracking,
        space_cognition_reinitializer=_reinitialize_space_cognition,
        session=SQLiteSession(
            (
                "midbrain-autonomous-agent-systemic-gui-v5-"
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
        provider_hot_readiness_timeout_s=(
            settings.provider_hot_readiness_timeout_s
        ),
        max_turns=settings.openai_agent_max_turns,
        session_history_item_limit=(
            settings.openai_agent_session_history_items
        ),
    )


driver = _build_autonomous_agent_driver()
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
pending_agent_runs: dict[str, PendingAgentRun] = {}
pending_agent_runs_lock = asyncio.Lock()
replay_capture = Phase5ReplayCaptureService(fabric, settings.replay_bundle_dir)
auto_initialization_task: asyncio.Task[None] | None = None
auto_initialization_error: str | None = None
auto_initialization_state = "NOT_STARTED"
auto_initialization_result: dict[str, Any] | None = None
scene_policy_restore_result: dict[str, Any] | None = None
scene_policy_restore_error: str | None = None


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
    global midbrain_session_id, midbrain_session_source
    global midbrain_session_identity_checked_at
    global scene_policy_restore_result, scene_policy_restore_error
    try:
        manager_identity = await asyncio.wait_for(
            manager.health(),
            timeout=2.0,
        )
        midbrain_session_identity_checked_at = time.monotonic()
        manager_boot_id = str(manager_identity.get("boot_id") or "").strip()
        if manager_boot_id:
            midbrain_session_id = manager_boot_id
            midbrain_session_source = "manager_boot_id"
    except Exception:
        pass
    await agent_run_journal.start(session_id=midbrain_session_id)
    try:
        scene_policy_restore_result = (
            await scene_segmentation_policy_publisher.restore_policy()
        )
        scene_policy_restore_error = None
    except Exception as error:
        scene_policy_restore_result = None
        scene_policy_restore_error = str(error)
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
    await agent_run_stream_registry.shutdown()
    await world_point_cloud.stop()
    await basic.close()
    await integrated.close()
    await fabric.close()
    await manager.close()
    await agent_run_journal.close()


app = FastAPI(
    title="Physical Agent Test Scaffold",
    version="0.4.9",
    lifespan=lifespan,
)

DEFAULT_SESSION_AUTO_SPEED_M_S = 5.0


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
    attachment_ids: list[str] = Field(
        default_factory=list,
        max_length=1,
    )
    auto_authorize_provider_activation: bool = False
    auto_authorize_provider_stop: bool = False
    auto_authorize_relative_motion: bool = False
    max_auto_move_cm: float = Field(default=120.0, ge=0.1, le=120.0)
    max_auto_speed_m_s: float = Field(
        default=DEFAULT_SESSION_AUTO_SPEED_M_S,
        gt=0.0,
    )
    auto_authorize_stationary_calibration: bool = False
    auto_authorize_stationary_activation: bool = False
    auto_authorize_safe_home: bool = False
    auto_authorize_space_reinitialization: bool = False


class ItemLocationRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    target_frame: str = Field(
        default="CURRENT_WORLD",
        min_length=1,
        max_length=200,
    )
    object_id: str | None = Field(default=None, max_length=200)
    contact_policy: str = Field(
        default="WORKPIECE_CONTACT_ALLOWED",
        pattern=r"^(WORKPIECE_CONTACT_ALLOWED|NO_CONTACT)$",
    )
    depth_requirement: str = Field(
        default="PREFER_METRIC",
        pattern=r"^(PREFER_METRIC|REQUIRE_METRIC|ALLOW_BEARING)$",
    )
    task_plane: dict[str, Any] | None = None


class RgbdPointRegistrationRequest(BaseModel):
    pixel_yx: list[float] = Field(min_length=2, max_length=2)
    target_frame: str = Field(
        default="CURRENT_WORLD",
        min_length=1,
        max_length=200,
    )
    depth_policy: str = Field(
        default="ROBUST_MEDIAN",
        pattern=r"^(ROBUST_MEDIAN|CLOSEST_TO_CAMERA|NEAREST_VALID_PIXEL)$",
    )


class AgentAttachmentUpload(BaseModel):
    filename: str = Field(default="user-image", max_length=255)
    media_type: str = Field(
        pattern=r"^image/(jpeg|png|webp)$",
        max_length=40,
    )
    data_base64: str = Field(min_length=1, max_length=11_200_000)


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
            r"AUTO_PROVIDER_STOP|"
            r"AUTO_BOUNDED_RELATIVE_MOTION|"
            r"AUTO_STATIONARY_CALIBRATION|"
            r"AUTO_STATIONARY_ACTIVATION|"
            r"AUTO_SAFE_HOME|AUTO_SPACE_REINITIALIZATION)$"
        ),
    )
    max_auto_move_cm: float | None = Field(
        default=None,
        ge=0.1,
        le=120.0,
    )
    max_auto_speed_m_s: float | None = Field(
        default=None,
        gt=0.0,
    )


def _approval_arguments(approval: dict[str, Any]) -> dict[str, Any]:
    canonical = approval.get("authorization_arguments")
    if isinstance(canonical, dict):
        return canonical
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
    tool_name = str(approval.get("tool_name") or "")
    arguments = dict(_approval_arguments(approval))
    if tool_name in {
        "execute_integrated_motion_preview",
        PERFORM_RELATIVE_EFFECTOR_MOTION_TOOL,
    }:
        arguments.pop("preview_id", None)
        tool_name = "integrated_relative_motion_commit"
    return json.dumps(
        {
            "tool_name": tool_name,
            "arguments": arguments,
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
        approval = (
            interruption
            if isinstance(interruption, dict)
            else PrototypeAgentDriver._approval_description(interruption)
        )
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
        interruption
        if isinstance(interruption, dict)
        else PrototypeAgentDriver._approval_description(interruption)
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
    if decision.approval_mode == "AUTO_PROVIDER_STOP":
        eligible = all(
            approval.get("tool_name") == "set_provider_residency"
            and str(
                _approval_arguments(approval).get("action") or ""
            ).lower()
            == "stop"
            for approval in approvals
        )
        if not eligible:
            raise HTTPException(
                status_code=409,
                detail=(
                    "The session Provider-stop authorization permits only "
                    "exact stop transitions; activation, motion, and other "
                    "protected operations retain their own policy."
                ),
            )
        return
    if decision.approval_mode == "AUTO_BOUNDED_RELATIVE_MOTION":
        maximum_cm = decision.max_auto_move_cm
        if maximum_cm is None or not math.isfinite(maximum_cm):
            raise HTTPException(
                status_code=422,
                detail="bounded motion authorization requires a finite cm limit",
            )
        eligible = all(
            approval.get("tool_name")
            in {
                "execute_integrated_motion_preview",
                PERFORM_RELATIVE_EFFECTOR_MOTION_TOOL,
            }
            and relative_motion_within_authorization(
                _approval_arguments(approval),
                max_auto_move_cm=maximum_cm,
                max_auto_speed_m_s=(
                    decision.max_auto_speed_m_s
                    or DEFAULT_SESSION_AUTO_SPEED_M_S
                ),
            )
            for approval in approvals
        )
        if not eligible:
            raise HTTPException(
                status_code=409,
                detail=(
                    "The requested operation is not an exact Integrated "
                    "relative-pose preview within the browser-authorized "
                    f"{maximum_cm:g} cm limit, the 10/20 rad/s per-joint "
                    "authentication policy, and the fixed 45-degree "
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
                    "mounted-rig activation; physical motion and protected "
                    "operations still require their own decision."
                ),
            )
        return
    if decision.approval_mode == "AUTO_SAFE_HOME":
        eligible = all(
            approval.get("tool_name") == "execute_basic_safe_home"
            for approval in approvals
        )
        if not eligible:
            raise HTTPException(
                status_code=409,
                detail=(
                    "The safe-home session authorization permits only the "
                    "exact Basic Controller safe-home operation."
                ),
            )
        return
    if decision.approval_mode == "AUTO_SPACE_REINITIALIZATION":
        eligible = all(
            approval.get("tool_name") == "reinitialize_space_cognition"
            for approval in approvals
        )
        if not eligible:
            raise HTTPException(
                status_code=409,
                detail=(
                    "The space-reinitialization session authorization "
                    "permits only a deliberate Local VIO epoch reset."
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
VISUAL_EVIDENCE_SCRIPT = (
    Path(__file__).resolve().parent / "web" / "visual_evidence.js"
).read_text(encoding="utf-8")
AGENT_CHAT_HISTORY_SCRIPT = (
    Path(__file__).resolve().parent / "web" / "agent_chat_history.js"
).read_text(encoding="utf-8")
RUN_JOURNAL_PAGE = (
    Path(__file__).resolve().parent / "web" / "run_journal.html"
).read_text(encoding="utf-8")
RUN_JOURNAL_SCRIPT = (
    Path(__file__).resolve().parent / "web" / "run_journal.js"
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


@app.get("/assets/visual_evidence.js")
async def visual_evidence_script() -> Response:
    return Response(
        content=VISUAL_EVIDENCE_SCRIPT,
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/assets/agent_chat_history.js")
async def agent_chat_history_script() -> Response:
    return Response(
        content=AGENT_CHAT_HISTORY_SCRIPT,
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/assets/run_journal.js")
async def run_journal_script() -> Response:
    return Response(
        content=RUN_JOURNAL_SCRIPT,
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/dev", response_class=HTMLResponse)
async def developer_index() -> str:
    return PAGE


@app.get("/dev/run-journal", response_class=HTMLResponse)
async def run_journal_index() -> str:
    return RUN_JOURNAL_PAGE


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


@app.get("/api/run-journal/sessions")
async def run_journal_sessions(limit: int = 100) -> Response:
    if not 1 <= limit <= 500:
        raise HTTPException(
            status_code=422,
            detail="run journal limit must be between 1 and 500",
        )
    try:
        sessions = await agent_run_journal.list_sessions(limit=limit)
    except Exception as error:
        raise HTTPException(
            status_code=503,
            detail=f"run journal is unavailable: {error}",
        ) from error
    return Response(
        content=json.dumps(
            {
                "journal": agent_run_journal.health_snapshot(),
                "sessions": sessions,
            },
            ensure_ascii=False,
        ),
        media_type="application/json",
        headers={"Cache-Control": "private, no-store"},
    )


@app.get("/api/run-journal/sessions/{session_id}")
async def run_journal_session(session_id: str) -> Response:
    normalized_session_id = session_id.strip()
    if not normalized_session_id or len(normalized_session_id) > 200:
        raise HTTPException(status_code=422, detail="invalid session id")
    try:
        detail = await agent_run_journal.get_session(
            normalized_session_id,
            run_limit=500,
        )
        if detail is None:
            raise HTTPException(
                status_code=404,
                detail="Midbrain session was not found",
            )
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(
            status_code=503,
            detail=f"run journal is unavailable: {error}",
        ) from error
    return Response(
        content=json.dumps(detail, ensure_ascii=False),
        media_type="application/json",
        headers={"Cache-Control": "private, no-store"},
    )


@app.get("/api/run-journal/runs/{run_id}")
async def run_journal_run(run_id: str) -> Response:
    normalized_run_id = run_id.strip()
    if not normalized_run_id or len(normalized_run_id) > 200:
        raise HTTPException(status_code=422, detail="invalid run id")
    try:
        run = await agent_run_journal.get_run(normalized_run_id)
        if run is None:
            raise HTTPException(
                status_code=404,
                detail="run journal record was not found",
            )
        events = await agent_run_journal.events(normalized_run_id)
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(
            status_code=503,
            detail=f"run journal is unavailable: {error}",
        ) from error
    return Response(
        content=json.dumps(
            {"run": run, "events": events},
            ensure_ascii=False,
        ),
        media_type="application/json",
        headers={"Cache-Control": "private, no-store"},
    )


def _chat_progress_label(event: dict[str, Any]) -> str | None:
    event_type = str(event.get("type") or "")
    payload = event.get("payload")
    values = payload if isinstance(payload, dict) else {}
    tool_name = str(values.get("tool_name") or "Agent tool")
    if event_type == "tool.called":
        return f"Started {tool_name}"
    if event_type == "tool.completed":
        return f"Completed {tool_name}"
    if event_type == "approval.required":
        return "Development approval required"
    if event_type == "approval.resolved":
        return "Development approval resolved"
    if event_type == "skill.retry.recovered":
        return "Observation recovered after a bounded retry"
    if event_type == "skill.retry.exhausted":
        return "Observation retry exhausted"
    if event_type == "visual.evidence.created":
        return "Visual evidence attached to the response"
    return None


def _chat_turn_projection(run: dict[str, Any]) -> dict[str, Any] | None:
    prompt = run.get("user_prompt")
    if prompt is None:
        return None
    events = run.get("events")
    safe_events = events if isinstance(events, list) else []
    reasoning = "".join(
        str((event.get("payload") or {}).get("text") or "")
        for event in safe_events
        if event.get("type") == "assistant.reasoning_summary.delta"
        and isinstance(event.get("payload"), dict)
    )
    progress = [
        {
            "label": label,
            "occurred_at": str(
                event.get("occurred_at") or run["updated_at"]
            ),
        }
        for event in safe_events
        if (label := _chat_progress_label(event)) is not None
    ]
    visual_evidence = next(
        (
            event.get("payload")
            for event in reversed(safe_events)
            if event.get("type") == "visual.evidence.created"
            and isinstance(event.get("payload"), dict)
        ),
        None,
    )
    status_value = str(run.get("status") or "INTERRUPTED").upper()
    status = status_value if status_value in {
        "RUNNING",
        "COMPLETED",
        "FAILED",
        "INTERRUPTED",
    } else "INTERRUPTED"
    return {
        "schema": "midbrain.agent_chat_turn.v1",
        "turn_id": str(run["run_id"]),
        "run_id": str(run["run_id"]),
        "prompt": str(prompt),
        "attachment_name": (
            "Attached image" if int(run.get("attachment_count") or 0) else ""
        ),
        "agent_model": str(run.get("agent_model") or ""),
        "reasoning_effort": str(run.get("reasoning_effort") or ""),
        "vlm_model": str(run.get("vlm_model") or ""),
        "started_at": str(run["started_at"]),
        "status": status,
        "activity": status.title(),
        "answer": str(run.get("assistant_answer") or ""),
        "reasoning": reasoning,
        "progress": progress[-80:],
        "event_details": safe_events,
        "visual_evidence": visual_evidence,
    }


@app.get("/api/chat-session")
async def current_chat_session() -> Response:
    await _refresh_midbrain_session_identity()
    try:
        detail = await agent_run_journal.get_session(
            midbrain_session_id,
            run_limit=40,
            include_events=True,
        )
    except Exception as error:
        raise HTTPException(
            status_code=503,
            detail=f"chat session is unavailable: {error}",
        ) from error
    session = detail["session"] if detail is not None else {
        "session_id": midbrain_session_id,
        "status": "ACTIVE",
    }
    turns = []
    for run in detail["runs"] if detail is not None else []:
        projected = _chat_turn_projection(run)
        if projected is not None:
            turns.append(projected)
    return Response(
        content=json.dumps(
            {"session": session, "turns": turns},
            ensure_ascii=False,
        ),
        media_type="application/json",
        headers={"Cache-Control": "private, no-store"},
    )


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
        "agent_runtime_session_epoch": agent_runtime_session_epoch,
        "midbrain_session_id": midbrain_session_id,
        "midbrain_session_source": midbrain_session_source,
        "agent_run_journal": agent_run_journal.health_snapshot(),
        "scene_policy_restore": {
            "state_path": str(settings.scene_policy_state_path),
            "result": scene_policy_restore_result,
            "error": scene_policy_restore_error,
        },
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
            transform = await _frame_transform_to_world(
                source_frame=frame_id,
                world_frame=world_frame,
                session_epoch=session_epoch,
                at_us=observed_at_us,
                max_extrapolation_us=500_000,
            )
            if transform is None:
                raise RuntimeError("world-frame identity was not handled")
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


@app.post("/api/spatial/items/locate")
async def locate_item_metric(
    request: ItemLocationRequest,
) -> dict[str, Any]:
    """Invoke the typed metric locator without conversational tool routing."""

    try:
        return await item_locator_skill.run(
            question=request.question,
            target_frame=request.target_frame,
            object_id=request.object_id,
            contact_policy=request.contact_policy,
            depth_requirement=request.depth_requirement,
            task_plane=request.task_plane,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except (RuntimeError, httpx.HTTPError) as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.post("/api/spatial/register-rgbd-point")
async def register_rgbd_point_metric(
    request: RgbdPointRegistrationRequest,
) -> dict[str, Any]:
    """Register one supplied RGB pixel through current synchronized depth."""

    try:
        return await spatial_registration_skill.run(
            pixel_yx=request.pixel_yx,
            target_frame=request.target_frame,
            depth_policy=request.depth_policy,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except (RuntimeError, httpx.HTTPError) as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


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


def _transform_annotation_center(
    center_m: list[Any],
    transform: dict[str, Any] | None,
) -> list[float]:
    center = [float(value) for value in center_m]
    if len(center) != 3 or not all(math.isfinite(value) for value in center):
        raise ValueError("annotation center must contain three finite values")
    if transform is None:
        return center
    translation = [float(value) for value in transform["translation_m"]]
    rotation = rotation_matrix(transform["rotation_xyzw"])
    return [
        translation[row]
        + sum(rotation[row][column] * center[column] for column in range(3))
        for row in range(3)
    ]


def _compose_fabric_transforms(
    outer: dict[str, Any],
    inner: dict[str, Any],
) -> dict[str, Any]:
    """Compose target-from-middle with middle-from-source."""

    inner_translation = [float(value) for value in inner["translation_m"]]
    outer_translation = [float(value) for value in outer["translation_m"]]
    outer_rotation = rotation_matrix(outer["rotation_xyzw"])
    translation = [
        outer_translation[row]
        + sum(
            outer_rotation[row][column] * inner_translation[column]
            for column in range(3)
        )
        for row in range(3)
    ]
    x2, y2, z2, w2 = [float(value) for value in outer["rotation_xyzw"]]
    x1, y1, z1, w1 = [float(value) for value in inner["rotation_xyzw"]]
    quaternion = [
        w2 * x1 + x2 * w1 + y2 * z1 - z2 * y1,
        w2 * y1 - x2 * z1 + y2 * w1 + z2 * x1,
        w2 * z1 + x2 * y1 - y2 * x1 + z2 * w1,
        w2 * w1 - x2 * x1 - y2 * y1 - z2 * z1,
    ]
    norm = math.sqrt(sum(value * value for value in quaternion))
    if norm <= 1e-12:
        raise ValueError("composed transform quaternion has zero norm")
    return {
        "from_frame": inner.get("from_frame"),
        "to_frame": outer.get("to_frame"),
        "at_us": outer.get("at_us") or inner.get("at_us"),
        "translation_m": translation,
        "rotation_xyzw": [value / norm for value in quaternion],
        "path": [
            *(inner.get("path") or []),
            *(outer.get("path") or []),
        ],
        "epoch_composition": "VIO_WORLD_WITH_INDEPENDENT_ARM_CONTROL_EPOCH",
    }


async def _frame_transform_to_world(
    *,
    source_frame: str,
    world_frame: str,
    session_epoch: str,
    at_us: int,
    max_extrapolation_us: int,
) -> dict[str, Any] | None:
    if source_frame == world_frame:
        return None
    arm_frames = {
        settings.arm_base_frame,
        settings.arm_tool_frame,
        *(f"link{index}" for index in range(1, 7)),
    }
    if source_frame not in arm_frames:
        return await fabric.transform(
            from_frame=source_frame,
            to_frame=world_frame,
            at_us=at_us,
            max_extrapolation_us=max_extrapolation_us,
            session_epoch=session_epoch,
        )
    world_from_arm_base = await fabric.transform(
        from_frame=settings.arm_base_frame,
        to_frame=world_frame,
        at_us=at_us,
        max_extrapolation_us=max_extrapolation_us,
        session_epoch=session_epoch,
    )
    if source_frame == settings.arm_base_frame:
        return world_from_arm_base
    arm_base_from_source = await fabric.transform(
        from_frame=source_frame,
        to_frame=settings.arm_base_frame,
        at_us=at_us,
        max_extrapolation_us=max_extrapolation_us,
        session_epoch=None,
    )
    return _compose_fabric_transforms(
        world_from_arm_base,
        arm_base_from_source,
    )


async def _annotation_frame_transform(
    *,
    source_frame: str,
    world_frame: str,
    session_epoch: str,
    at_us: int,
) -> dict[str, Any] | None:
    if source_frame == world_frame:
        return None
    if source_frame.startswith("local_vio/"):
        raise RuntimeError(
            "annotation belongs to a different Local VIO world epoch"
        )
    return await _frame_transform_to_world(
        source_frame=source_frame,
        world_frame=world_frame,
        session_epoch=session_epoch,
        at_us=at_us,
        max_extrapolation_us=750_000,
    )


@app.get("/api/world-annotations")
async def world_annotations() -> dict[str, Any]:
    """Project semantic spheres, the last item, and the gripper into the viewer."""

    point_cloud_status = await world_point_cloud.status()
    world_frame = str(point_cloud_status.get("world_frame") or "")
    session_epoch = str(point_cloud_status.get("session_epoch") or "")
    if not world_frame or not session_epoch:
        raise HTTPException(
            status_code=409,
            detail="world point-cloud epoch is not established",
        )
    now_us = time.time_ns() // 1000
    markers: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    scene_metadata: dict[str, Any] = {"status": "NO_SCENE"}
    gripper_metadata: dict[str, Any] = {
        "status": "UNAVAILABLE",
        "frame_id": settings.arm_tool_frame,
    }

    vio_observation = await fabric.latest_optional("localization.vio.status")
    vio_data = (
        vio_observation.get("data")
        if isinstance(vio_observation, dict)
        else None
    )
    if isinstance(vio_data, dict):
        vio_epoch = str(vio_data.get("session_epoch") or "")
        gripper_at_us = int(vio_observation.get("observed_at_us") or 0)
        if vio_epoch != session_epoch:
            warnings.append(
                "gripper marker VIO epoch does not match the point-cloud epoch"
            )
        elif gripper_at_us <= 0:
            warnings.append("gripper marker has no VIO observation timestamp")
        else:
            try:
                gripper_transform = await _frame_transform_to_world(
                    source_frame=settings.arm_tool_frame,
                    world_frame=world_frame,
                    session_epoch=session_epoch,
                    at_us=gripper_at_us,
                    max_extrapolation_us=750_000,
                )
                if gripper_transform is None:
                    raise RuntimeError("gripper transform resolved as identity")
                markers["robot-gripper-tool"] = {
                    "marker_id": "robot-gripper-tool",
                    "label": "gripper / tool frame",
                    "center_m": [
                        float(value)
                        for value in gripper_transform["translation_m"]
                    ],
                    "radius_m": 0.025,
                    "type": "GRIPPER",
                    "roi_scope": None,
                    "semantic_source": "ROBOT_KINEMATIC_TRANSFORM",
                    "source": "FABRIC_TRANSFORM",
                    "stale": False,
                    "show_label": True,
                    "observed_at_us": gripper_at_us,
                }
                gripper_metadata = {
                    "status": "CURRENT",
                    "frame_id": settings.arm_tool_frame,
                    "observed_at_us": gripper_at_us,
                    "transform_path": gripper_transform.get("path") or [],
                }
            except Exception as error:
                gripper_metadata = {
                    "status": "UNAVAILABLE",
                    "frame_id": settings.arm_tool_frame,
                    "error": str(error),
                }
                warnings.append(f"gripper marker unavailable: {error}")

    scene = await fabric.latest_optional("robot_arm.primary.integrated.scene")
    if isinstance(scene, dict):
        data = scene.get("data")
        data = data if isinstance(data, dict) else {}
        source_frame = str(data.get("frame_id") or "")
        spheres = data.get("spheres")
        spheres = spheres if isinstance(spheres, list) else []
        expires_at_us = int(scene.get("expires_at_us") or 0)
        scene_stale = expires_at_us > 0 and expires_at_us <= now_us
        scene_metadata = {
            "status": "STALE" if scene_stale else "CURRENT",
            "scene_revision": data.get("scene_revision"),
            "source_frame": source_frame,
            "sphere_count": len(spheres),
            "expires_at_us": expires_at_us or None,
        }
        try:
            transform = await _annotation_frame_transform(
                source_frame=source_frame,
                world_frame=world_frame,
                session_epoch=session_epoch,
                at_us=now_us,
            )
            semantic = [
                value
                for value in spheres
                if isinstance(value, dict)
                and str(value.get("type") or "KEEP_OUT") != "KEEP_OUT"
            ]
            keep_out = [
                value
                for value in spheres
                if isinstance(value, dict)
                and str(value.get("type") or "KEEP_OUT") == "KEEP_OUT"
            ]
            # The controller consumes the complete scene. The viewer uses a
            # deterministic reduced layer so dense table coverage remains
            # legible and inexpensive to evaluate interactively.
            keep_out_limit = max(0, 240 - len(semantic))
            keep_out_step = max(
                1,
                math.ceil(len(keep_out) / max(1, keep_out_limit)),
            )
            displayed_keep_out = keep_out[::keep_out_step][
                :keep_out_limit
            ]
            selected = [
                *semantic,
                *displayed_keep_out,
            ]
            scene_metadata.update(
                {
                    "displayed_sphere_count": len(selected),
                    "displayed_keep_out_count": len(displayed_keep_out),
                    "keep_out_display_stride": keep_out_step,
                    "visualization_limit": 240,
                    "visualization_reduced": len(selected) < len(spheres),
                }
            )
            for sphere in selected:
                marker_id = str(
                    sphere.get("sphere_id")
                    or sphere.get("object_id")
                    or f"scene-sphere-{len(markers)}"
                )
                radius_m = float(sphere.get("radius_m") or 0.0)
                if not math.isfinite(radius_m) or radius_m <= 0.0:
                    continue
                markers[marker_id] = {
                    "marker_id": marker_id,
                    "label": str(
                        sphere.get("object_id") or marker_id
                    ),
                    "center_m": _transform_annotation_center(
                        list(sphere.get("center_m") or []),
                        transform,
                    ),
                    "radius_m": radius_m,
                    "type": str(sphere.get("type") or "KEEP_OUT"),
                    "roi_scope": sphere.get("roi_scope"),
                    "semantic_source": sphere.get("semantic_source"),
                    "source": "ARM_SCENE_COMPILER",
                    "stale": scene_stale,
                    "show_label": sphere in semantic,
                }
        except Exception as error:
            warnings.append(f"compiled scene projection unavailable: {error}")

    item = item_locator_skill.last_metric_result
    if isinstance(item, dict) and item.get("eligible_for_control_math") is True:
        location = item.get("location")
        location = location if isinstance(location, dict) else {}
        object_id = str(item.get("object_id") or "last-metric-item")
        source_frame = str(item.get("target_frame") or "")
        capture = item.get("camera_capture")
        capture = capture if isinstance(capture, dict) else {}
        item_epoch = str(capture.get("session_epoch") or "").strip()
        if item_epoch.lower() in {"none", "null"}:
            item_epoch = ""
        if item_epoch and item_epoch != session_epoch:
            warnings.append(
                "last trusted metric item belongs to a different VIO epoch"
            )
            item = None
    if isinstance(item, dict) and item.get("eligible_for_control_math") is True:
        location = item.get("location")
        location = location if isinstance(location, dict) else {}
        object_id = str(item.get("object_id") or "last-metric-item")
        source_frame = str(item.get("target_frame") or "")
        try:
            transform = await _annotation_frame_transform(
                source_frame=source_frame,
                world_frame=world_frame,
                session_epoch=session_epoch,
                at_us=now_us,
            )
            volume = item.get("volume_hint")
            volume = volume if isinstance(volume, dict) else {}
            radius_m = max(
                float(location.get("uncertainty_radius_m") or 0.0),
                float(
                    volume.get("representative_sphere_radius_m")
                    or volume.get("raw_sphere_radius_m")
                    or 0.0
                ),
                0.005,
            )
            item_observed_at_us = int(item.get("observed_at_us") or 0)
            item_age_ms = (
                None
                if item_observed_at_us <= 0
                else max(0.0, (now_us - item_observed_at_us) / 1000.0)
            )
            markers[object_id] = {
                "marker_id": object_id,
                "label": str(item.get("item_label") or object_id),
                "center_m": _transform_annotation_center(
                    list(
                        volume.get("estimated_centroid_target_m")
                        or location.get("target_point_m")
                        or []
                    ),
                    transform,
                ),
                "radius_m": radius_m,
                "type": "WORK_OBJECT",
                "roi_scope": None,
                "semantic_source": "METRIC_ITEM_LOCATOR",
                "source": "LAST_TRUSTED_METRIC_ITEM_RESULT",
                "stale": item_age_ms is None or item_age_ms > 60_000.0,
                "show_label": True,
                "observed_at_us": item.get("observed_at_us"),
                "age_ms": item_age_ms,
                "uncertainty_radius_m": location.get(
                    "uncertainty_radius_m"
                ),
            }
        except Exception as error:
            warnings.append(f"last metric item projection unavailable: {error}")

    return {
        "schema": "physical_agent.world_annotation_snapshot",
        "schema_version": 1,
        "world_frame": world_frame,
        "session_epoch": session_epoch,
        "observed_at_us": now_us,
        "scene": scene_metadata,
        "gripper": gripper_metadata,
        "marker_count": len(markers),
        "markers": list(markers.values()),
        "warnings": warnings,
    }


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


async def _agent_input(request: PromptRequest) -> AgentInput:
    attachments = []
    for attachment_id in request.attachment_ids:
        try:
            attachments.append(
                await agent_attachment_store.read(attachment_id)
            )
        except KeyError as error:
            raise HTTPException(
                status_code=410,
                detail=(
                    "agent image attachment was not found or expired; "
                    "upload it again"
                ),
            ) from error
    return build_multimodal_user_input(request.prompt, attachments)


def _session_authorization(
    request: PromptRequest,
) -> AgentSessionAuthorization:
    return AgentSessionAuthorization(
        auto_authorize_provider_activation=(
            request.auto_authorize_provider_activation
        ),
        auto_authorize_provider_stop=request.auto_authorize_provider_stop,
        auto_authorize_relative_motion=request.auto_authorize_relative_motion,
        max_auto_move_cm=request.max_auto_move_cm,
        max_auto_speed_m_s=request.max_auto_speed_m_s,
        auto_authorize_stationary_calibration=(
            request.auto_authorize_stationary_calibration
        ),
        auto_authorize_stationary_activation=(
            request.auto_authorize_stationary_activation
        ),
        auto_authorize_safe_home=request.auto_authorize_safe_home,
        auto_authorize_space_reinitialization=(
            request.auto_authorize_space_reinitialization
        ),
    )


@app.post("/api/streaming-runs", status_code=202)
async def start_streaming_run(request: PromptRequest) -> dict[str, Any]:
    """Start a backend-owned run and return an independently replayable SSE URL."""

    await _refresh_midbrain_session_identity()
    run_id = str(uuid.uuid4())
    agent_model, reasoning_effort, vlm_model = _model_selection(request)
    authorization = _session_authorization(request)
    input_value = await _agent_input(request)
    await agent_run_journal.record_turn(
        run_id,
        session_id=midbrain_session_id,
        prompt=request.prompt,
        agent_model=agent_model,
        reasoning_effort=reasoning_effort,
        vlm_model=vlm_model or "auto",
        attachment_count=len(request.attachment_ids),
    )
    channel = await agent_run_stream_registry.create(run_id)
    await channel.publish(
        "run.started",
        {
            "surface": "autonomous",
            "midbrain_session_id": midbrain_session_id,
            "agent_model": agent_model,
            "reasoning_effort": reasoning_effort,
            "vlm_model": vlm_model or "auto",
            "attachment_count": len(request.attachment_ids),
        },
    )
    await channel.set_status("RUNNING")
    await agent_run_stream_registry.launch(
        run_id,
        _run_streaming_autonomous_agent(
            input_value,
            run_id,
            agent_model=agent_model,
            reasoning_effort=reasoning_effort,
            vlm_model=vlm_model,
            authorization=authorization,
        ),
    )
    return {
        "status": "running",
        "run_id": run_id,
        "events_url": f"/api/streaming-runs/{run_id}/events",
        "status_url": f"/api/streaming-runs/{run_id}",
        "decision_url": f"/api/streaming-runs/{run_id}/decision",
    }


@app.get("/api/streaming-runs/{run_id}")
async def streaming_run_status(run_id: str) -> dict[str, Any]:
    try:
        channel = await agent_run_stream_registry.get(run_id)
    except KeyError as error:
        raise HTTPException(
            status_code=404,
            detail="streaming agent run was not found or expired",
        ) from error
    return await channel.snapshot()


@app.get("/api/streaming-runs/{run_id}/events")
async def streaming_run_events(
    run_id: str,
    request: Request,
    after: int = 0,
) -> StreamingResponse:
    try:
        channel = await agent_run_stream_registry.get(run_id)
    except KeyError as error:
        raise HTTPException(
            status_code=404,
            detail="streaming agent run was not found or expired",
        ) from error
    last_event_id = parse_event_sequence(
        request.headers.get("last-event-id")
    )
    after_sequence = max(0, int(after), last_event_id)
    return StreamingResponse(
        stream_sse(channel, after_sequence=after_sequence),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/streaming-runs/{run_id}/decision", status_code=202)
async def decide_streaming_run(
    run_id: str,
    decision: DeveloperApprovalDecision,
) -> dict[str, Any]:
    try:
        channel = await agent_run_stream_registry.get(run_id)
    except KeyError as error:
        raise HTTPException(
            status_code=404,
            detail="streaming agent run was not found or expired",
        ) from error
    snapshot = await channel.snapshot()
    if snapshot["status"] != "AWAITING_APPROVAL":
        raise HTTPException(
            status_code=409,
            detail="streaming agent run is not awaiting approval",
        )
    async with pending_agent_runs_lock:
        entry = pending_agent_runs.pop(run_id, None)
    if entry is None:
        raise HTTPException(
            status_code=404,
            detail="pending autonomous agent run was not found or expired",
        )
    if time.monotonic() - entry.created_monotonic > 600.0:
        await channel.publish(
            "run.failed",
            {"error": "pending autonomous agent approval expired"},
        )
        await channel.set_status(
            "FAILED",
            result={
                "status": "failed",
                "run_id": run_id,
                "error": "pending autonomous agent approval expired",
            },
        )
        raise HTTPException(
            status_code=410,
            detail="pending autonomous agent approval expired",
        )
    interruptions = entry.state.get_interruptions()
    if not interruptions:
        raise HTTPException(
            status_code=409,
            detail="autonomous agent run has no pending approvals",
        )
    approval_descriptions = [
        await driver._approval_description_with_pending(interruption)
        for interruption in interruptions
    ]
    try:
        _validate_automatic_agent_approval(
            approval_descriptions,
            decision,
        )
    except HTTPException:
        async with pending_agent_runs_lock:
            pending_agent_runs[run_id] = entry
        raise
    approval_decisions = _record_approval_decisions(
        approval_descriptions,
        approved=decision.approve,
        existing=entry.approval_decisions,
    )
    for interruption in interruptions:
        if decision.approve:
            entry.state.approve(interruption)
        else:
            await driver.discard_pending_prepared_action(interruption)
            entry.state.reject(
                interruption,
                rejection_message=decision.rejection_message,
            )
    await channel.publish(
        "approval.resolved",
        {
            "approved": decision.approve,
            "approval_mode": decision.approval_mode,
        },
    )
    await channel.set_status("RUNNING")
    await agent_run_stream_registry.launch(
        run_id,
        _run_streaming_autonomous_agent(
            entry.state,
            run_id,
            agent_model=entry.agent_model,
            reasoning_effort=entry.reasoning_effort,
            vlm_model=entry.vlm_model,
            authorization=entry.authorization,
            approval_decisions=approval_decisions,
        ),
    )
    return {
        "status": "running",
        "run_id": run_id,
        "approved": decision.approve,
    }


async def _run_streaming_autonomous_agent(
    input_value: AgentInput,
    run_id: str,
    *,
    agent_model: str,
    reasoning_effort: str,
    vlm_model: str | None,
    authorization: AgentSessionAuthorization,
    approval_decisions: dict[str, bool] | None = None,
) -> None:
    channel = await agent_run_stream_registry.get(run_id)

    async def publish(
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        await channel.publish(event_type, payload)

    try:
        result = await _autonomous_agent_step(
            input_value,
            run_id,
            agent_model=agent_model,
            reasoning_effort=reasoning_effort,
            vlm_model=vlm_model,
            authorization=authorization,
            approval_decisions=approval_decisions,
            event_sink=publish,
        )
    except Exception as error:
        detail = (
            error.response.text
            if isinstance(error, httpx.HTTPStatusError)
            and error.response.text
            else str(error)
        )
        failed = {
            "status": "failed",
            "run_id": run_id,
            "error": detail,
        }
        await channel.publish("run.failed", {"error": detail})
        await channel.set_status("FAILED", result=failed)
        return

    if result["status"] == "approval_required":
        await channel.publish(
            "approval.required",
            {
                "status": result["status"],
                "run_id": run_id,
                "approvals": result["approvals"],
            },
        )
        await channel.set_status("AWAITING_APPROVAL", result=result)
        return
    await channel.publish(
        "run.completed",
        {
            "status": result["status"],
            "answer": result.get("answer"),
            "approval_loop_prevented": result.get(
                "approval_loop_prevented",
                False,
            ),
        },
    )
    await channel.set_status("COMPLETED", result=result)


async def _autonomous_agent_step(
    input_value: AgentInput,
    run_id: str,
    *,
    agent_model: str,
    reasoning_effort: str,
    vlm_model: str | None,
    authorization: AgentSessionAuthorization,
    approval_decisions: dict[str, bool] | None = None,
    event_sink: AgentEventSink | None = None,
) -> dict[str, Any]:
    result = await operation_registry.run(
        f"openai_autonomous_agent_run:{run_id}",
        driver.run_interactive(
            input_value,
            model_override=agent_model,
            reasoning_effort=reasoning_effort,
            vlm_model_override=vlm_model,
            authorization=authorization,
            event_sink=event_sink,
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
    async with pending_agent_runs_lock:
        pending_agent_runs[run_id] = PendingAgentRun(
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


@app.post("/api/agent-attachments", status_code=201)
async def upload_agent_attachment(
    request: AgentAttachmentUpload,
) -> dict[str, object]:
    try:
        attachment = await agent_attachment_store.register_base64(
            data_base64=request.data_base64,
            media_type=request.media_type,
            filename=request.filename,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return attachment.public_metadata()


@app.get("/api/agent-attachments/{attachment_id}")
async def agent_attachment_preview(attachment_id: str) -> Response:
    try:
        attachment = await agent_attachment_store.read(attachment_id)
    except KeyError as error:
        raise HTTPException(
            status_code=404,
            detail="agent image attachment was not found or expired",
        ) from error
    return Response(
        content=attachment.data,
        media_type=attachment.media_type,
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/api/visual-evidence/{evidence_id}/channels/{channel_id}")
async def visual_evidence_channel(
    evidence_id: str,
    channel_id: str,
) -> Response:
    try:
        channel = await visual_evidence_store.read(evidence_id, channel_id)
    except KeyError as error:
        raise HTTPException(
            status_code=404,
            detail="visual evidence channel was not found or expired",
        ) from error
    return Response(
        content=channel.data,
        media_type=channel.media_type,
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


def main() -> None:
    uvicorn.run(app, host=settings.ui_host, port=settings.ui_port, log_level="info")


PAGE = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Midbrain Autonomous Agent</title>
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
    main { max-width: 1900px; margin: 0 auto; padding: 24px; }
    body.developer-workspace { height: 100dvh; overflow: hidden; }
    body.developer-workspace main { display: flex; height: 100%; flex-direction: column; padding: 14px 18px 18px; }
    body.developer-workspace .developer-nav-bar { margin-bottom: 10px !important; }
    body.developer-workspace h1 { margin-top: 2px; font-size: 1.72rem; }
    body.developer-workspace .sub { margin-bottom: 12px; font-size: 12px; }
    h1 { margin-bottom: 4px; }
    h2 { margin-top: 4px; }
    .sub { color: var(--mb-muted); margin-top: 0; }
    .role-kicker { color: var(--mb-accent); font-size: 11px; font-weight: 800; letter-spacing: .16em; text-transform: uppercase; margin-bottom: 6px; }
    .grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); min-height: 0; gap: 18px; }
    body.developer-workspace .grid { flex: 1; }
    .dev-pane { min-width: 0; min-height: 0; overflow-y: auto; overscroll-behavior: contain; scrollbar-gutter: stable; padding-right: 7px; }
    .conversation-pane { overflow: hidden; padding-right: 0; }
    .card { background: rgba(19,19,19,.96); border: 1px solid var(--mb-border); border-radius: 14px; padding: 16px; margin-bottom: 18px; }
    .diagnostic-card { margin-top: 0; padding: 0; color: var(--mb-text); }
    .diagnostic-card > summary { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 14px 16px; color: var(--mb-text); font-weight: 700; }
    .diagnostic-card > summary small { color: var(--mb-muted); font-size: 10px; font-weight: 550; letter-spacing: .08em; text-transform: uppercase; }
    .diagnostic-card[open] > summary { border-bottom: 1px solid var(--mb-border); }
    .diagnostic-body { padding: 15px 16px 16px; }
    #agentPromptPanel { display: grid; grid-template-rows: auto minmax(0, 1fr) auto; height: 100%; min-height: 0; margin: 0; overflow: hidden; padding: 0; }
    .developer-conversation-head { padding: 14px 16px 10px; }
    .developer-conversation-head .chat-panel-head { margin: 0; }
    .developer-chat-scroll { min-height: 0; overflow: hidden; border-top: 1px solid var(--mb-border); border-bottom: 1px solid var(--mb-border); background: #0d0d0d; }
    .developer-chat-scroll .chat-history-empty { margin: 22px 16px; }
    .developer-chat-scroll .chat-history { display: flex; height: 100%; max-height: none; flex-direction: column; padding: 12px 9px 12px 14px; }
    .chat-history-spacer { flex: 1 1 auto; min-height: 0; }
    .developer-composer { max-height: 48vh; overflow-y: auto; padding: 13px 16px 15px; scrollbar-gutter: stable; }
    .developer-composer label[for="prompt"] { display: block; margin-bottom: 6px; color: var(--mb-muted); font-size: 11px; font-weight: 700; }
    .developer-composer textarea { min-height: 86px; }
    .developer-composer-actions { display: flex; justify-content: flex-end; }
    .developer-composer-actions button { margin-left: 0; }
    .developer-composer .authorization-controls { margin-top: 10px; }
    .authorization-disclosure { margin-top: 10px; }
    .authorization-disclosure > summary { color: var(--mb-muted); font-size: 11px; }
    .model-controls { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin: 10px 0 12px; }
    .model-controls label { color: var(--mb-muted); font-size: 11px; }
    .authorization-controls { display: grid; gap: 9px; margin-top: 14px; border-top: 1px solid var(--mb-border); padding-top: 13px; }
    .authorization-toggle { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; margin: 0; color: var(--mb-secondary); font-size: 12px; font-weight: 500; }
    .authorization-toggle input[type="checkbox"] { width: 16px; height: 16px; accent-color: var(--mb-accent); }
    .authorization-toggle input[type="number"] { width: 72px; border: 1px solid var(--mb-border); border-radius: 6px; padding: 6px 7px; background: var(--mb-bg); color: var(--mb-text); font: inherit; }
    .authorization-ticker { margin: 2px 0 0; color: var(--mb-muted); font-size: 11px; line-height: 1.5; }
    .agent-activity { margin: 10px 0 0; color: var(--mb-muted); font-size: 12px; }
    .chat-panel-head { display: flex; align-items: start; justify-content: space-between; gap: 12px; margin-top: 16px; }
    .chat-panel-head h2 { margin: 0 0 4px; }
    .chat-panel-head .agent-activity { margin: 0; }
    .chat-panel-head button { margin: 0; padding: 7px 10px; background: transparent; color: var(--mb-muted); font-size: 11px; }
    .chat-history-empty { margin: 22px 0; color: var(--mb-muted); font-size: 13px; text-align: center; }
    .chat-history { display: grid; gap: 15px; max-height: min(72vh, 980px); overflow-y: auto; overscroll-behavior: contain; padding: 2px 7px 2px 0; scrollbar-gutter: stable; }
    .chat-turn { display: grid; gap: 8px; }
    .chat-message { border: 1px solid var(--mb-border); border-radius: 10px; padding: 12px; }
    .chat-message-user { width: min(92%, 720px); margin-left: auto; background: var(--mb-surface-raised); }
    .chat-message-assistant { background: #101010; }
    .chat-message-head { display: flex; align-items: baseline; flex-wrap: wrap; gap: 8px; margin-bottom: 8px; color: var(--mb-muted); font-size: 10px; }
    .chat-message-head strong { color: var(--mb-secondary); font-size: 11px; text-transform: uppercase; letter-spacing: .08em; }
    .chat-model-label { margin-left: auto; }
    .chat-message-text { margin: 0; line-height: 1.55; white-space: pre-wrap; }
    .chat-attachment { display: flex; align-items: center; gap: 8px; margin-top: 10px; color: var(--mb-muted); font-size: 11px; }
    .chat-attachment img { width: 58px; height: 58px; border: 1px solid var(--mb-border); border-radius: 7px; object-fit: cover; background: #050505; }
    .chat-turn-activity { margin: 0 0 9px; color: var(--mb-muted); font-size: 11px; }
    .chat-answer { min-height: 0; color: var(--mb-secondary); }
    .reasoning-panel { margin: 12px 0 0; border-top: 1px solid var(--mb-border); padding-top: 10px; }
    .reasoning-panel pre { min-height: 0; max-height: 240px; color: var(--mb-muted); }
    .chat-progress-list { display: grid; gap: 6px; margin: 10px 0 0; padding-left: 22px; color: var(--mb-muted); font-size: 11px; }
    .chat-progress-list li time { float: right; margin-left: 12px; color: #777; }
    .chat-execution-summary h4 { margin: 12px 0 0; color: var(--mb-muted); font-size: 11px; font-weight: 650; }
    .chat-event-details { margin: 10px 0 0; border-top: 1px solid var(--mb-border); padding-top: 10px; color: var(--mb-muted); }
    .chat-event-details > summary { font-size: 11px; font-weight: 650; }
    .chat-event-list { display: grid; gap: 6px; margin-top: 9px; }
    .chat-event-record { margin: 0; border: 1px solid var(--mb-border); border-radius: 7px; background: #0b0b0b; }
    .chat-event-record > summary { padding: 7px 9px; font-size: 10px; overflow-wrap: anywhere; }
    .chat-event-record > pre { min-height: 0; max-height: 360px; margin: 0 7px 7px; border: 1px solid #2f2f2f; padding: 9px; background: #070707; color: #cfcfcf; font-size: 10px; }
    .visual-evidence { margin: 12px 0 0; border: 1px solid var(--mb-border); border-radius: 10px; padding: 12px; background: #101010; }
    .visual-evidence-head { display: flex; align-items: start; justify-content: space-between; gap: 12px; margin-bottom: 9px; }
    .visual-evidence-head h3 { margin: 0 0 3px; font-size: 14px; }
    .visual-evidence-head p { margin: 0; color: var(--mb-muted); font-size: 11px; }
    .visual-toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-bottom: 9px; }
    .visual-toolbar button { margin: 0; padding: 6px 9px; background: #252525; color: var(--mb-text); font-size: 11px; }
    .visual-toolbar button.active { outline: 1px solid var(--mb-accent); }
    .visual-toolbar label { display: inline-flex; align-items: center; gap: 5px; color: var(--mb-muted); font-size: 11px; }
    .visual-toolbar input[type="color"] { width: 28px; height: 24px; padding: 0; border: 0; background: transparent; }
    .visual-annotation-colors { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
    .visual-color-control { max-width: 190px; padding: 3px 7px; border: 1px solid var(--mb-border); border-radius: 999px; background: #181818; }
    .visual-color-control span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .visual-canvas { position: relative; width: 100%; overflow: hidden; border-radius: 8px; background: #050505; }
    .visual-canvas img, .visual-canvas svg { position: absolute; inset: 0; width: 100%; height: 100%; }
    .visual-canvas img { object-fit: contain; }
    .visual-canvas svg { pointer-events: none; }
    select { width: 100%; margin-top: 5px; padding: 8px; border: 1px solid var(--mb-border); border-radius: 7px; background: var(--mb-bg); color: var(--mb-text); }
    textarea { width: 100%; min-height: 100px; resize: vertical; padding: 12px; border-radius: 8px; border: 1px solid var(--mb-border); background: var(--mb-bg); color: var(--mb-text); }
    .agent-attachment-picker { display: flex; flex-wrap: wrap; align-items: center; gap: 9px; margin-top: 9px; }
    .agent-attachment-picker input[type="file"] { position: absolute; width: 1px; height: 1px; overflow: hidden; opacity: 0; }
    .agent-attachment-button { display: inline-flex; border: 1px solid var(--mb-border); border-radius: 7px; padding: 7px 10px; background: var(--mb-surface-raised); color: var(--mb-text); cursor: pointer; font-size: 12px; }
    .agent-attachment-hint { color: var(--mb-muted); font-size: 11px; }
    .agent-attachment-preview { display: flex; align-items: center; gap: 8px; min-width: 0; }
    .agent-attachment-preview img { width: 44px; height: 44px; min-height: 0; border: 1px solid var(--mb-border); border-radius: 7px; object-fit: cover; }
    .agent-attachment-preview span { max-width: 240px; overflow: hidden; color: var(--mb-secondary); font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
    .agent-attachment-preview button { margin: 0; padding: 6px 8px; border: 1px solid var(--mb-border); background: transparent; color: var(--mb-muted); font-size: 11px; }
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
    .annotation-overlay { position: absolute; right: 10px; top: 50px; max-width: 48%; background: rgba(5,5,5,.78); padding: 7px 9px; border-radius: 6px; font-size: 12px; pointer-events: none; text-align: right; }
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
    .annotation-controls { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 14px; margin: 0 0 10px; border: 1px solid var(--mb-border); border-radius: 8px; padding: 8px 10px; background: #101010; }
    .annotation-toggle { display: inline-flex; align-items: center; gap: 6px; color: var(--mb-secondary); font-size: 11px; white-space: nowrap; }
    .annotation-toggle input { margin: 0; accent-color: var(--mb-accent); }
    .annotation-swatch { width: 10px; height: 10px; border-radius: 50%; box-shadow: 0 0 0 1px rgba(255,255,255,.24); }
    .annotation-swatch.keep-out { background: #ff3d3d; }
    .annotation-swatch.pushable { background: #f259e0; }
    .annotation-swatch.work-object { background: #ffb82e; }
    .annotation-swatch.gripper { background: #33e6ff; }
    .annotation-legend-note { margin-left: auto; color: var(--mb-muted); font-size: 10px; }
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
    .annotation-label { color: #ffd166; border-color: rgba(255,209,102,.55); }
    .screen-axis-overlay { position: absolute; right: 10px; bottom: 10px; width: 184px; height: 116px; border-radius: 8px; background: rgba(5,5,5,.88); border: 1px solid rgba(255,255,255,.18); pointer-events: none; }
    .screen-axis-overlay svg { width: 100%; height: 100%; }
    .screen-axis-overlay text { fill: #d7d7d7; font: 10px system-ui, sans-serif; }
    body.focused-utility-mode { height: auto; min-height: 100vh; overflow: auto; }
    body.focused-utility-mode main { display: block; height: auto; max-width: 1900px; }
    body.focused-utility-mode .grid { display: block; }
    body.focused-utility-mode .dev-pane { overflow: visible; padding-right: 0; }
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
    @media (max-width: 980px) {
      body.developer-workspace { height: auto; min-height: 100dvh; overflow: auto; }
      body.developer-workspace main { height: auto; min-height: 100dvh; }
      .grid { grid-template-columns: 1fr; }
      .dev-pane { max-height: 68dvh; }
      .conversation-pane { min-height: 72dvh; }
      .viewer-wrap { height: 55vh; }
    }
    @media (max-width: 640px) { .sensor-grid, .catalog-grid, .model-controls { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
<main>
  <nav class="developer-nav-bar" style="display:flex;justify-content:space-between;gap:16px;margin-bottom:24px">
    <a href="http://127.0.0.1:7001/" style="color:var(--mb-muted);text-decoration:none">← Midbrain</a>
    <span style="display:flex;gap:16px">
      <a href="/dev/run-journal" style="color:var(--mb-muted);text-decoration:none">Run journal</a>
      <a id="secondaryNav" href="/" style="color:var(--mb-warning);text-decoration:none">Regular agent</a>
    </span>
  </nav>
  <div class="role-kicker">Developer view</div>
  <h1 id="pageTitle">Autonomous Agent · Developer view</h1>
  <p class="sub" id="pageSubtitle">The same autonomous Agent as the regular page, with additional Provider, Skill, replay, point-cloud, and normalized-event diagnostics.</p>
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
        <textarea id="prompt" placeholder="Describe the task for the agent."></textarea>
        <div class="model-controls">
          <label>Agent model<select id="agentModel"><option value="gpt-5.6-terra">GPT-5.6 Terra</option></select></label>
          <label>Reasoning<select id="reasoningEffort"><option value="medium">Medium</option></select></label>
          <label>Visual model<select id="vlmModel"><option value="auto">Auto routing</option></select></label>
        </div>
        <div class="agent-attachment-picker">
          <label class="agent-attachment-button" for="developerAgentImageInput">Attach image</label>
          <input id="developerAgentImageInput" type="file" accept="image/jpeg,image/png,image/webp">
          <span id="developerAttachmentHint" class="agent-attachment-hint">Optional JPEG, PNG, or WebP; maximum 8 MB.</span>
          <div id="developerAttachmentPreview" class="agent-attachment-preview" hidden>
            <img id="developerAttachmentPreviewImage" alt="Selected user image">
            <span id="developerAttachmentName"></span>
            <button id="removeDeveloperAttachment" type="button">Remove</button>
          </div>
        </div>
        <div>
          <button class="primary" id="run">Run prompt</button>
        </div>
        <div class="authorization-controls" aria-label="Session authorization">
          <label class="authorization-toggle">
            <input id="autoApproveProviders" type="checkbox" checked>
            Auto-authorize Provider start, HOT, and WARM transitions
          </label>
          <label class="authorization-toggle">
            <input id="autoApproveProviderStop" type="checkbox" checked>
            Auto-authorize Provider stop transitions
          </label>
          <label class="authorization-toggle">
            <input id="autoApproveMoves" type="checkbox" checked>
            Auto-authorize each exact relative arm pose preview: translation up to
            <input id="maxAutoMoveCm" type="number" min="0.1" max="120" step="0.1" value="120" inputmode="decimal" aria-label="Maximum automatically authorized move in centimeters">
            <input id="maxAutoSpeedMps" type="hidden" value="5">
            cm; per-joint speed asks above 10 rad/s and hard-stops at 20 rad/s;
            controlled-frame yaw up to 45°
          </label>
          <label class="authorization-toggle">
            <input id="autoApproveCalibration" type="checkbox" checked>
            Auto-authorize stationary world-to-arm calibration
          </label>
          <label class="authorization-toggle">
            <input id="autoApproveCalibrationActivation" type="checkbox" checked>
            Auto-authorize exact qualified calibration candidate activation
          </label>
          <label class="authorization-toggle">
            <input id="autoApproveSafeHome" type="checkbox" checked>
            Auto-authorize the exact controller-owned safe-home operation
          </label>
          <label class="authorization-toggle">
            <input id="autoApproveSpaceReinitialization" type="checkbox">
            Auto-authorize deliberate spatial-origin reinitialization
          </label>
          <p id="authorizationTicker" class="authorization-ticker"></p>
        </div>
        <div class="chat-panel-head">
          <div>
            <h2>Conversation</h2>
            <p id="agentActivity" class="agent-activity">Ready.</p>
          </div>
        </div>
        <p id="developerChatHistoryEmpty" class="chat-history-empty">No runs in this Midbrain session.</p>
        <div id="developerChatHistory" class="chat-history" aria-live="polite"></div>
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
        <div class="annotation-controls" aria-label="World annotation visibility and color legend">
          <label class="annotation-toggle"><input id="showAnnotations" type="checkbox" checked>Show markers</label>
          <label class="annotation-toggle"><input id="showKeepOut" type="checkbox" checked><span class="annotation-swatch keep-out"></span>Obstacle / keep-out</label>
          <label class="annotation-toggle"><input id="showPushable" type="checkbox" checked><span class="annotation-swatch pushable"></span>Pushable</label>
          <label class="annotation-toggle"><input id="showWorkObject" type="checkbox" checked><span class="annotation-swatch work-object"></span>Work object</label>
          <label class="annotation-toggle"><input id="showGripper" type="checkbox" checked><span class="annotation-swatch gripper"></span>Gripper</label>
          <span class="annotation-legend-note">Only user-declared KEEP_OUT geometry is blocking; unclaimed visible geometry is ignored PUSHABLE telemetry.</span>
        </div>
        <div class="viewer-wrap">
          <canvas id="cloud"></canvas>
          <div class="viewer-overlay" id="cloudStats">Waiting for pose and RGB-D…</div>
          <div class="annotation-overlay" id="annotationStats">Waiting for semantic annotations…</div>
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
        <p class="controls">Orthographic world view. Drag to orbit; Shift-drag, middle-drag, or right-drag to pan; use the wheel to zoom. Frame checkboxes control live local XYZ triads: red +X, green +Y, blue +Z. Orange remains gravity/down (-Z), cyan is the camera frustum, and point-cloud samples fade over 10 seconds. Marker colors and visibility are controlled by the legend above.</p>
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
<script src="/assets/visual_evidence.js"></script>
<script src="/assets/agent_chat_history.js"></script>
<script>
const dedicatedSpaceCognition = location.pathname.endsWith(
  '/dev/skills/initialize-space-cognition'
);
const dedicatedSpatialAxes = location.pathname.endsWith('/dev/spatial-axes');
const focusedUtilityPage = dedicatedSpaceCognition || dedicatedSpatialAxes;
document.body.classList.toggle('focused-utility-mode', focusedUtilityPage);
const pageTitle = document.getElementById('pageTitle');
const pageSubtitle = document.getElementById('pageSubtitle');
const secondaryNav = document.getElementById('secondaryNav');
const spaceCognitionLinkPanel = document.getElementById('spaceCognitionLinkPanel');
const spaceCognitionPanel = document.getElementById('spaceCognitionPanel');
const agentPromptPanel = document.getElementById('agentPromptPanel');
const replayPanel = document.getElementById('replayPanel');
const platformCatalogPanel = document.getElementById('platformCatalogPanel');
const worldPointCloudPanel = document.getElementById('worldPointCloudPanel');
if (dedicatedSpaceCognition) {
  document.title = 'Space Cognition Development | Midbrain';
  pageTitle.textContent = 'Space Cognition Development';
  pageSubtitle.textContent =
    'Initialize or deliberately re-establish the local spatial epoch, inspect VIO state, and review the accumulated world model.';
  secondaryNav.href = '/dev';
  secondaryNav.textContent = 'Developer view';
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
  secondaryNav.textContent = 'Developer view';
  spaceCognitionPanel.hidden = true;
  agentPromptPanel.hidden = true;
  replayPanel.hidden = true;
  platformCatalogPanel.hidden = true;
  spaceCognitionLinkPanel.hidden = true;
} else {
  document.title = 'Autonomous Agent Developer View | Midbrain';
  spaceCognitionPanel.hidden = true;
}

function collapsibleDiagnostic(panel, open = false) {
  if (!panel || panel.hidden) return;
  const heading = panel.querySelector(':scope > h2');
  const kicker = panel.querySelector(':scope > .role-kicker');
  const details = document.createElement('details');
  details.className = 'card diagnostic-card';
  details.open = open;
  const summary = document.createElement('summary');
  const title = document.createElement('span');
  title.textContent = heading?.textContent || 'Developer diagnostic';
  const category = document.createElement('small');
  category.textContent = kicker?.textContent || 'Diagnostics';
  summary.append(title, category);
  heading?.remove();
  kicker?.remove();
  panel.className = 'diagnostic-body';
  panel.replaceWith(details);
  details.append(summary, panel);
}

function modernizeDeveloperWorkspace() {
  document.body.classList.add('developer-workspace');
  const grid = document.querySelector('.grid');
  const diagnosticsPane = grid?.children[0];
  const conversationPane = grid?.children[1];
  if (!grid || !diagnosticsPane || !conversationPane) return;
  diagnosticsPane.id = 'developerDiagnosticsPane';
  diagnosticsPane.className = 'dev-pane diagnostics-pane';
  conversationPane.id = 'developerConversationPane';
  conversationPane.className = 'dev-pane conversation-pane';
  diagnosticsPane.append(
    worldPointCloudPanel,
    replayPanel,
    platformCatalogPanel,
    spaceCognitionLinkPanel
  );
  conversationPane.replaceChildren(agentPromptPanel);
  for (const panel of [
    worldPointCloudPanel,
    replayPanel,
    platformCatalogPanel,
    spaceCognitionLinkPanel
  ]) {
    collapsibleDiagnostic(panel, panel === worldPointCloudPanel);
  }

  const promptLabel = document.createElement('label');
  promptLabel.htmlFor = 'prompt';
  promptLabel.textContent = 'Prompt';
  const modelControls = agentPromptPanel.querySelector('.model-controls');
  const attachmentPicker = agentPromptPanel.querySelector(
    '.agent-attachment-picker'
  );
  const runButtonWrapper = agentPromptPanel.querySelector('#run').parentElement;
  const authorizationControls = agentPromptPanel.querySelector(
    '.authorization-controls'
  );
  const chatHead = agentPromptPanel.querySelector('.chat-panel-head');
  const chatEmpty = document.getElementById('developerChatHistoryEmpty');
  const chatHistory = document.getElementById('developerChatHistory');
  const oldKicker = agentPromptPanel.querySelector(':scope > .role-kicker');
  const oldHeading = agentPromptPanel.querySelector(':scope > h2');
  oldKicker?.remove();
  oldHeading?.remove();

  const head = document.createElement('div');
  head.className = 'developer-conversation-head';
  head.appendChild(chatHead);
  const chatScroll = document.createElement('div');
  chatScroll.className = 'developer-chat-scroll';
  const spacer = document.createElement('div');
  spacer.className = 'chat-history-spacer';
  spacer.setAttribute('aria-hidden', 'true');
  chatHistory.prepend(spacer);
  chatScroll.append(chatEmpty, chatHistory);
  const composer = document.createElement('div');
  composer.className = 'developer-composer';
  runButtonWrapper.className = 'developer-composer-actions';
  const authorizationDisclosure = document.createElement('details');
  authorizationDisclosure.className = 'authorization-disclosure';
  const authorizationDisclosureTitle = document.createElement('summary');
  authorizationDisclosureTitle.textContent = 'Development autonomy policy';
  authorizationDisclosure.append(
    authorizationDisclosureTitle,
    authorizationControls
  );
  composer.append(
    promptLabel,
    document.getElementById('prompt'),
    modelControls,
    attachmentPicker,
    runButtonWrapper,
    authorizationDisclosure
  );
  agentPromptPanel.replaceChildren(head, chatScroll, composer);
}

if (!focusedUtilityPage) modernizeDeveloperWorkspace();
const runButton = document.getElementById('run');
const refreshReplayButton = document.getElementById('refreshReplay');
const initializeButton = document.getElementById('initialize');
const resetButton = document.getElementById('reset');
const clearCloudButton = document.getElementById('clearCloud');
const resetViewButton = document.getElementById('resetView');
const fitAxesButton = document.getElementById('fitAxes');
const axisControls = document.getElementById('axisControls');
const axisStatus = document.getElementById('axisStatus');
const showAnnotations = document.getElementById('showAnnotations');
const showKeepOut = document.getElementById('showKeepOut');
const showPushable = document.getElementById('showPushable');
const showWorkObject = document.getElementById('showWorkObject');
const showGripper = document.getElementById('showGripper');
const frameLabels = document.getElementById('frameLabels');
const screenAxisOverlay = document.getElementById('screenAxisOverlay');
const authorizationDialog = document.getElementById('authorizationDialog');
const authorizationTitle = document.getElementById('authorizationTitle');
const authorizationSummary = document.getElementById('authorizationSummary');
const authorizationDetails = document.getElementById('authorizationDetails');
const approveAuthorization = document.getElementById('approveAuthorization');
const denyAuthorization = document.getElementById('denyAuthorization');
const promptBox = document.getElementById('prompt');
const developerAgentImageInput = document.getElementById(
  'developerAgentImageInput'
);
const developerAttachmentHint = document.getElementById(
  'developerAttachmentHint'
);
const developerAttachmentPreview = document.getElementById(
  'developerAttachmentPreview'
);
const developerAttachmentPreviewImage = document.getElementById(
  'developerAttachmentPreviewImage'
);
const developerAttachmentName = document.getElementById(
  'developerAttachmentName'
);
const removeDeveloperAttachment = document.getElementById(
  'removeDeveloperAttachment'
);
const agentActivity = document.getElementById('agentActivity');
const developerChatHistory = new window.MidbrainAgentChatHistory({
  container: document.getElementById('developerChatHistory'),
  emptyState: document.getElementById('developerChatHistoryEmpty'),
  detailedEvents: true,
  onStatus: (message) => agentActivity.textContent = message
});
const agentModel = document.getElementById('agentModel');
const reasoningEffort = document.getElementById('reasoningEffort');
const vlmModel = document.getElementById('vlmModel');
const autoApproveProviders = document.getElementById('autoApproveProviders');
const autoApproveProviderStop = document.getElementById(
  'autoApproveProviderStop'
);
const autoApproveMoves = document.getElementById('autoApproveMoves');
const autoApproveCalibration = document.getElementById('autoApproveCalibration');
const autoApproveCalibrationActivation = document.getElementById('autoApproveCalibrationActivation');
const autoApproveSafeHome = document.getElementById('autoApproveSafeHome');
const autoApproveSpaceReinitialization = document.getElementById(
  'autoApproveSpaceReinitialization'
);
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
const DEVELOPER_AGENT_IMAGE_TYPES = new Set([
  'image/jpeg',
  'image/png',
  'image/webp'
]);
const DEVELOPER_AGENT_IMAGE_MAX_BYTES = 8 * 1024 * 1024;
let developerSelectedImage = null;
let developerSelectedImageUrl = null;

function clearDeveloperSelectedImage() {
  if (developerSelectedImageUrl) {
    URL.revokeObjectURL(developerSelectedImageUrl);
  }
  developerSelectedImageUrl = null;
  developerSelectedImage = null;
  developerAgentImageInput.value = '';
  developerAttachmentPreviewImage.removeAttribute('src');
  developerAttachmentName.textContent = '';
  developerAttachmentPreview.hidden = true;
  developerAttachmentHint.hidden = false;
}

function selectDeveloperImage(file) {
  if (!file) {
    clearDeveloperSelectedImage();
    return;
  }
  if (!DEVELOPER_AGENT_IMAGE_TYPES.has(file.type)) {
    throw new Error('Choose a JPEG, PNG, or WebP image.');
  }
  if (file.size > DEVELOPER_AGENT_IMAGE_MAX_BYTES) {
    throw new Error('The attached image exceeds 8 MB.');
  }
  clearDeveloperSelectedImage();
  developerSelectedImage = file;
  developerSelectedImageUrl = URL.createObjectURL(file);
  developerAttachmentPreviewImage.src = developerSelectedImageUrl;
  developerAttachmentName.textContent =
    file.name + ' | ' + (file.size / 1024).toFixed(0) + ' KB';
  developerAttachmentPreview.hidden = false;
  developerAttachmentHint.hidden = true;
}

function readDeveloperImageAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error('Could not read the image.'));
    reader.onload = () => {
      const value = String(reader.result || '');
      const separator = value.indexOf(',');
      if (separator < 0) {
        reject(new Error('Could not encode the image.'));
        return;
      }
      resolve(value.slice(separator + 1));
    };
    reader.readAsDataURL(file);
  });
}

async function uploadDeveloperImage() {
  if (!developerSelectedImage) return [];
  agentActivity.textContent = 'Uploading image attachment...';
  const response = await fetch('/api/agent-attachments', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      filename: developerSelectedImage.name,
      media_type: developerSelectedImage.type,
      data_base64: await readDeveloperImageAsBase64(
        developerSelectedImage
      )
    })
  });
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.detail || 'Image upload failed');
  }
  return [data.attachment_id];
}

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
  autoApproveProviderStop.checked =
    typeof saved.autoApproveProviderStop === 'boolean'
      ? saved.autoApproveProviderStop
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
  autoApproveSafeHome.checked =
    typeof saved.autoApproveSafeHome === 'boolean'
      ? saved.autoApproveSafeHome
      : true;
  autoApproveSpaceReinitialization.checked =
    typeof saved.autoApproveSpaceReinitialization === 'boolean'
      ? saved.autoApproveSpaceReinitialization
      : false;
  const savedMaximum = Number(saved.maxAutoMoveCm);
  maxAutoMoveCm.value =
    Number.isFinite(savedMaximum) && savedMaximum >= 0.1 &&
    savedMaximum <= 120 ? String(savedMaximum) : '120';
  const savedMaximumSpeed = Number(saved.maxAutoSpeedMps);
  maxAutoSpeedMps.value =
    Number.isFinite(savedMaximumSpeed) && savedMaximumSpeed > 0 &&
    Number.isFinite(savedMaximumSpeed) && savedMaximumSpeed > 0
      ? String(savedMaximumSpeed)
      : '5';
  updateAgentAuthorizationTicker();
}

function agentAuthorizationPreferences() {
  const maximum = Number(maxAutoMoveCm.value);
  const maximumSpeed = Number(maxAutoSpeedMps.value);
  return {
    autoApproveProviders: autoApproveProviders.checked,
    autoApproveProviderStop: autoApproveProviderStop.checked,
    autoApproveMoves: autoApproveMoves.checked,
    autoApproveCalibration: autoApproveCalibration.checked,
    autoApproveCalibrationActivation:
      autoApproveCalibrationActivation.checked,
    autoApproveSafeHome: autoApproveSafeHome.checked,
    autoApproveSpaceReinitialization:
      autoApproveSpaceReinitialization.checked,
    maxAutoMoveCm:
      Number.isFinite(maximum) && maximum >= 0.1 && maximum <= 120
        ? maximum
        : 120,
    maxAutoSpeedMps:
      Number.isFinite(maximumSpeed) && maximumSpeed > 0 &&
      Number.isFinite(maximumSpeed) && maximumSpeed > 0
        ? maximumSpeed
        : 5
  };
}

function updateAgentAuthorizationTicker() {
  const preferences = agentAuthorizationPreferences();
  maxAutoMoveCm.disabled = !preferences.autoApproveMoves;
  const providerState = preferences.autoApproveProviders
    ? 'Provider activation AUTO'
    : 'Provider activation asks';
  const providerStopState = preferences.autoApproveProviderStop
    ? 'Provider stop AUTO'
    : 'Provider stop asks';
  const motionState = preferences.autoApproveMoves
    ? 'relative arm pose AUTO <= ' + preferences.maxAutoMoveCm + ' cm; ' +
      'joint speed asks >10 rad/s and hard-stops >=20 rad/s; ' +
      'controlled-frame yaw AUTO <= 45°'
    : 'physical motion asks';
  const calibrationState = preferences.autoApproveCalibration
    ? 'world-arm calibration AUTO'
    : 'world-arm calibration asks';
  const activationState = preferences.autoApproveCalibrationActivation
    ? 'exact calibration activation AUTO'
    : 'exact calibration activation asks';
  const safeHomeState = preferences.autoApproveSafeHome
    ? 'safe-home AUTO'
    : 'safe-home asks';
  const reinitializationState = preferences.autoApproveSpaceReinitialization
    ? 'spatial reinitialization AUTO'
    : 'spatial reinitialization asks';
  authorizationTicker.textContent =
    providerState + ' | ' + providerStopState + ' | ' + motionState + ' | ' +
    calibrationState + ' | ' + activationState + ' | ' + safeHomeState +
    ' | ' + reinitializationState + '. Provider and controller safety ' +
    'checks remain active.';
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
  const canonical = approval.authorization_arguments;
  if (canonical && typeof canonical === 'object') return canonical;
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
  const providerStopEligible =
    preferences.autoApproveProviderStop &&
    approvals.every((approval) => {
      const action = String(agentApprovalArguments(approval).action || '')
        .toLowerCase();
      return approval.tool_name === 'set_provider_residency' &&
        action === 'stop';
    });
  if (providerStopEligible) {
    return {
      approval_mode: 'AUTO_PROVIDER_STOP',
      max_auto_move_cm: null,
      max_auto_speed_m_s: null,
      label: 'Session authorization automatically approved Provider stop.'
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
        Number.isFinite(plannedSpeedMps) && plannedSpeedMps > 0;
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
      return [
        'execute_integrated_motion_preview',
        'perform_relative_effector_motion'
      ].includes(approval.tool_name) &&
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
  const safeHomeEligible =
    preferences.autoApproveSafeHome &&
    approvals.every(
      (approval) => approval.tool_name === 'execute_basic_safe_home'
    );
  if (safeHomeEligible) {
    return {
      approval_mode: 'AUTO_SAFE_HOME',
      max_auto_move_cm: null,
      max_auto_speed_m_s: null,
      label:
        'Session authorization automatically approved controller-owned safe-home.'
    };
  }
  const reinitializationEligible =
    preferences.autoApproveSpaceReinitialization &&
    approvals.every(
      (approval) => approval.tool_name === 'reinitialize_space_cognition'
    );
  if (reinitializationEligible) {
    return {
      approval_mode: 'AUTO_SPACE_REINITIALIZATION',
      max_auto_move_cm: null,
      max_auto_speed_m_s: null,
      label:
        'Session authorization automatically approved spatial reinitialization.'
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

function developerApprovalSummary(approvals) {
  return approvals.map((approval) => {
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
}

let chatSessionLoadPending = false;
async function loadChatSession() {
  if (focusedUtilityPage) return;
  if (chatSessionLoadPending) return;
  chatSessionLoadPending = true;
  try {
    const response = await fetch('/api/chat-session', {cache: 'no-store'});
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.detail || 'Chat session could not be loaded');
    }
    developerChatHistory.hydrate(data.turns || []);
  } catch (error) {
    agentActivity.textContent = 'History unavailable: ' + error.message;
  } finally {
    chatSessionLoadPending = false;
  }
}

function developerRunRequest(attachmentIds) {
  const authorization = agentAuthorizationPreferences();
  return {
    prompt: promptBox.value,
    attachment_ids: attachmentIds,
    agent_model: agentModel.value,
    reasoning_effort: reasoningEffort.value,
    vlm_model: vlmModel.value,
    auto_authorize_provider_activation:
      authorization.autoApproveProviders,
    auto_authorize_provider_stop:
      authorization.autoApproveProviderStop,
    auto_authorize_relative_motion: authorization.autoApproveMoves,
    max_auto_move_cm: authorization.maxAutoMoveCm,
    max_auto_speed_m_s: authorization.maxAutoSpeedMps,
    auto_authorize_stationary_calibration:
      authorization.autoApproveCalibration,
    auto_authorize_stationary_activation:
      authorization.autoApproveCalibrationActivation,
    auto_authorize_safe_home: authorization.autoApproveSafeHome,
    auto_authorize_space_reinitialization:
      authorization.autoApproveSpaceReinitialization
  };
}

async function submitStreamingDeveloperApproval(started, approvals) {
  const summary = developerApprovalSummary(approvals);
  const automaticDecision = automaticDeveloperApprovalDecision(approvals);
  const approved = automaticDecision ? true : window.confirm(summary);
  agentActivity.textContent = automaticDecision
    ? automaticDecision.label + ' Continuing the same run...'
    : approved
      ? 'Approval accepted. Continuing the same run...'
      : 'Operation rejected. Returning the decision to the agent...';
  const decisionUrl = started.decision_url ||
    '/api/streaming-runs/' + encodeURIComponent(started.run_id) +
    '/decision';
  const response = await fetch(decisionUrl, {
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
  });
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.detail || JSON.stringify(data));
  }
}

function consumeStreamingDeveloperRun(started, turn) {
  return new Promise((resolve, reject) => {
    const source = new EventSource(started.events_url);
    const handledApprovals = new Set();
    let answerText = '';
    let terminal = false;
    turn.setRunId(started.run_id);

    function fail(error) {
      if (terminal) return;
      terminal = true;
      source.close();
      const failure = error instanceof Error
        ? error
        : new Error(String(error));
      turn.fail(failure);
      reject(failure);
    }

    source.onopen = () => {
      agentActivity.textContent = 'Live autonomous Agent stream connected';
      turn.setActivity('Live autonomous Agent stream connected');
    };
    source.onerror = () => {
      if (!terminal) {
        agentActivity.textContent =
          'Live stream interrupted; reconnecting without restarting the run...';
      }
    };
    source.onmessage = async (message) => {
      try {
        const event = JSON.parse(message.data);
        turn.addEvent(event);
        const payload = event.payload || {};
        if (event.type === 'run.started') {
          agentActivity.textContent = 'Autonomous Agent run started';
          turn.setActivity('Autonomous Agent run started');
          turn.addProgress('Autonomous Agent run started');
        } else if (event.type === 'assistant.message.delta') {
          answerText += String(payload.text || '');
          turn.appendAnswer(String(payload.text || ''));
        } else if (
          event.type === 'assistant.reasoning_summary.delta'
        ) {
          turn.appendReasoning(String(payload.text || ''));
        } else if (event.type === 'tool.called') {
          const name = payload.tool_name || 'agent tool';
          agentActivity.textContent = 'Running ' + name + '...';
          turn.setActivity('Running ' + name + '...');
          turn.addProgress('Started ' + name);
        } else if (event.type === 'tool.completed') {
          const name = payload.tool_name || 'Agent tool';
          agentActivity.textContent = name + ' completed';
          turn.setActivity(name + ' completed');
          turn.addProgress('Completed ' + name);
        } else if (event.type === 'skill.retry.recovered') {
          const update =
            'Camera observation recovered after ' +
            payload.attempt_count + ' attempts';
          agentActivity.textContent = update;
          turn.setActivity(update);
          turn.addProgress(update);
        } else if (event.type === 'skill.retry.exhausted') {
          const update =
            'Camera observation unavailable after ' +
            payload.attempt_count + ' attempts';
          agentActivity.textContent = update;
          turn.setActivity(update);
          turn.addProgress(update);
        } else if (event.type === 'visual.evidence.created') {
          turn.showVisualEvidence(payload);
          agentActivity.textContent = 'Visual evidence available';
          turn.setActivity('Visual evidence available');
          turn.addProgress('Visual evidence attached to the response');
        } else if (event.type === 'agent.updated') {
          turn.addProgress(
            'Active agent: ' + (payload.agent_name || 'agent')
          );
        } else if (event.type === 'approval.required') {
          turn.addProgress('Development approval required');
          const approvalKey = String(
            event.event_id || message.lastEventId
          );
          if (!handledApprovals.has(approvalKey)) {
            handledApprovals.add(approvalKey);
            await submitStreamingDeveloperApproval(
              started,
              payload.approvals || []
            );
          }
        } else if (event.type === 'run.completed') {
          terminal = true;
          source.close();
          turn.complete(
            payload.answer || answerText ||
              'Completed without a text response.'
          );
          agentActivity.textContent = 'Completed';
          resolve(payload);
        } else if (event.type === 'run.failed') {
          fail(new Error(payload.error || 'Autonomous Agent run failed'));
        }
      } catch (error) {
        fail(error);
      }
    };
  });
}

runButton.addEventListener('click', async () => {
  const prompt = promptBox.value.trim();
  if (!prompt) return;
  const turn = developerChatHistory.startTurn({
    prompt,
    attachmentFile: developerSelectedImage,
    agentModel: agentModel.value,
    reasoningEffort: reasoningEffort.value,
    vlmModel: vlmModel.value
  });
  runButton.disabled = true;
  agentActivity.textContent = 'Starting backend-owned Agent run...';
  try {
    const attachmentIds = await uploadDeveloperImage();
    const requestPayload = developerRunRequest(attachmentIds);
    const response = await fetch('/api/streaming-runs', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(requestPayload)
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.detail || JSON.stringify(data));
    }
    await consumeStreamingDeveloperRun(data, turn);
    clearDeveloperSelectedImage();
    refreshStatus();
  } catch (error) {
    if (turn.state.status === 'RUNNING') turn.fail(error);
    agentActivity.textContent = 'Autonomous Agent run failed';
  } finally {
    runButton.disabled = false;
  }
});
promptBox.addEventListener('keydown', (event) => {
  if (
    event.key !== 'Enter' ||
    event.shiftKey ||
    event.isComposing
  ) return;
  event.preventDefault();
  if (!runButton.disabled) runButton.click();
});
developerAgentImageInput.addEventListener('change', (event) => {
  try {
    selectDeveloperImage(event.target.files?.[0] || null);
  } catch (error) {
    clearDeveloperSelectedImage();
    agentActivity.textContent = 'Error: ' + error.message;
  }
});
removeDeveloperAttachment.addEventListener(
  'click',
  clearDeveloperSelectedImage
);
refreshReplayButton.addEventListener('click', refreshReplayProvenance);

for (const control of [
  autoApproveProviders,
  autoApproveProviderStop,
  autoApproveMoves,
  autoApproveCalibration,
  autoApproveCalibrationActivation,
  autoApproveSafeHome,
  autoApproveSpaceReinitialization,
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
let worldAnnotationGroups = [];
let worldAnnotationMarkers = [];
let worldAnnotationSnapshotMarkers = [];
let worldAnnotationSnapshot = null;
let axisUiSignature = '';
const AXIS_VISIBILITY_KEY = 'midbrain.spatial-axis-visibility.v2';
const ANNOTATION_VISIBILITY_KEY = 'midbrain.world-annotation-visibility.v1';
const axisVisibility = new Map();
try {
  const storedVisibility = JSON.parse(localStorage.getItem(AXIS_VISIBILITY_KEY) || '{}');
  for (const [frameId, visible] of Object.entries(storedVisibility)) {
    axisVisibility.set(frameId, Boolean(visible));
  }
} catch (_error) {
  localStorage.removeItem(AXIS_VISIBILITY_KEY);
}
const annotationVisibilityControls = {
  ALL: showAnnotations,
  KEEP_OUT: showKeepOut,
  PUSHABLE: showPushable,
  WORK_OBJECT: showWorkObject,
  GRIPPER: showGripper
};
try {
  const storedVisibility = JSON.parse(
    localStorage.getItem(ANNOTATION_VISIBILITY_KEY) || '{}'
  );
  for (const [type, control] of Object.entries(annotationVisibilityControls)) {
    if (typeof storedVisibility[type] === 'boolean') {
      control.checked = storedVisibility[type];
    }
  }
} catch (_error) {
  localStorage.removeItem(ANNOTATION_VISIBILITY_KEY);
}

function annotationIsVisible(marker) {
  if (!showAnnotations.checked) return false;
  const control = annotationVisibilityControls[String(marker.type || '')];
  return control ? control.checked : true;
}

function updateAnnotationVisibility() {
  const state = Object.fromEntries(
    Object.entries(annotationVisibilityControls).map(
      ([type, control]) => [type, control.checked]
    )
  );
  localStorage.setItem(ANNOTATION_VISIBILITY_KEY, JSON.stringify(state));
  for (const control of [
    showKeepOut, showPushable, showWorkObject, showGripper
  ]) {
    control.disabled = !showAnnotations.checked;
  }
  rebuildWorldAnnotationBuffers(worldAnnotationSnapshotMarkers);
  updateAnnotationStats();
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

const annotationLabelNodes = new Map();
function syncAnnotationLabels(markers) {
  const labeled = markers.filter(marker => marker.show_label);
  const activeIds = new Set(labeled.map(marker => marker.marker_id));
  for (const [markerId, node] of annotationLabelNodes.entries()) {
    if (!activeIds.has(markerId)) {
      node.remove();
      annotationLabelNodes.delete(markerId);
    }
  }
  for (const marker of labeled) {
    let node = annotationLabelNodes.get(marker.marker_id);
    if (!node) {
      node = document.createElement('span');
      node.className = 'frame-label annotation-label';
      frameLabels.append(node);
      annotationLabelNodes.set(marker.marker_id, node);
    }
    node.textContent = marker.label + ' · ' + marker.type;
    node.title = JSON.stringify(marker, null, 2);
  }
}

function sphereLineVertices(center, radius, segments) {
  const values = [];
  for (const [axisA, axisB] of [[0, 1], [0, 2], [1, 2]]) {
    for (let index = 0; index < segments; index += 1) {
      const first = 2 * Math.PI * index / segments;
      const second = 2 * Math.PI * (index + 1) / segments;
      for (const angle of [first, second]) {
        const point = [...center];
        point[axisA] += Math.cos(angle) * radius;
        point[axisB] += Math.sin(angle) * radius;
        values.push(...point);
      }
    }
  }
  const crossRadius = Math.max(radius, 0.012);
  values.push(
    center[0] - crossRadius, center[1], center[2], center[0] + crossRadius, center[1], center[2],
    center[0], center[1] - crossRadius, center[2], center[0], center[1] + crossRadius, center[2],
    center[0], center[1], center[2] - crossRadius, center[0], center[1], center[2] + crossRadius
  );
  return values;
}

function rebuildWorldAnnotationBuffers(markers) {
  if (!gl) return;
  for (const group of worldAnnotationGroups) gl.deleteBuffer(group.buffer);
  worldAnnotationGroups = [];
  worldAnnotationSnapshotMarkers = markers.filter(marker =>
    Array.isArray(marker.center_m) && marker.center_m.length === 3 &&
    marker.center_m.every(value => Number.isFinite(Number(value))) &&
    Number(marker.radius_m) > 0
  ).map(marker => ({
    ...marker,
    center_m: marker.center_m.map(Number),
    radius_m: Number(marker.radius_m)
  }));
  worldAnnotationMarkers = worldAnnotationSnapshotMarkers.filter(
    annotationIsVisible
  );
  const styles = {
    WORK_OBJECT: [1.0, 0.72, 0.18, 0.96],
    PUSHABLE: [0.95, 0.35, 0.88, 0.92],
    KEEP_OUT: [1.0, 0.24, 0.24, 0.40],
    GRIPPER: [0.20, 0.90, 1.0, 1.0]
  };
  for (const type of ['KEEP_OUT', 'PUSHABLE', 'WORK_OBJECT', 'GRIPPER']) {
    const values = [];
    for (const marker of worldAnnotationMarkers.filter(item => item.type === type)) {
      values.push(...sphereLineVertices(
        marker.center_m,
        marker.radius_m,
        type === 'KEEP_OUT' ? 10 : 22
      ));
    }
    if (!values.length) continue;
    const buffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(values), gl.DYNAMIC_DRAW);
    worldAnnotationGroups.push({
      buffer,
      vertexCount: values.length / 3,
      color: styles[type]
    });
  }
  syncAnnotationLabels(worldAnnotationMarkers);
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
  for (const marker of worldAnnotationMarkers) {
    const node = annotationLabelNodes.get(marker.marker_id);
    if (!node) continue;
    const cameraPoint = transformMat4(view, [...marker.center_m, 1]);
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
  for (const group of worldAnnotationGroups) {
    gl.uniform4fv(gl.getUniformLocation(lineProgram, 'uColor'), group.color);
    gl.bindBuffer(gl.ARRAY_BUFFER, group.buffer);
    gl.vertexAttribPointer(linePosition, 3, gl.FLOAT, false, 0, 0);
    gl.drawArrays(gl.LINES, 0, group.vertexCount);
  }
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

async function refreshWorldAnnotations() {
  const annotationStats = document.getElementById('annotationStats');
  try {
    const response = await fetch('/api/world-annotations?t=' + Date.now(), {cache: 'no-store'});
    if (!response.ok) throw new Error(await response.text());
    const snapshot = await response.json();
    worldAnnotationSnapshot = snapshot;
    const markers = Array.isArray(snapshot.markers) ? snapshot.markers : [];
    rebuildWorldAnnotationBuffers(markers);
    updateAnnotationStats();
  } catch (error) {
    annotationStats.textContent = 'Semantic annotations: ' + error;
  }
}

function updateAnnotationStats() {
  const annotationStats = document.getElementById('annotationStats');
  if (!worldAnnotationSnapshot) return;
  const counts = worldAnnotationSnapshotMarkers.reduce((result, marker) => {
    const key = String(marker.type || 'UNKNOWN');
    result[key] = (result[key] || 0) + 1;
    return result;
  }, {});
  const summary = Object.entries(counts)
    .map(([key, count]) => `${count} ${key}`)
    .join(' · ');
  const sceneStatus = String(
    worldAnnotationSnapshot.scene?.status || 'NO_SCENE'
  );
  const gripperStatus = String(
    worldAnnotationSnapshot.gripper?.status || 'UNAVAILABLE'
  );
  const sourceSceneCount = Number(
    worldAnnotationSnapshot.scene?.sphere_count || 0
  );
  const displayedSceneCount = Number(
    worldAnnotationSnapshot.scene?.displayed_sphere_count || 0
  );
  const sceneDisplay = sourceSceneCount > 0
    ? ` · scene display ${displayedSceneCount}/${sourceSceneCount}`
    : '';
  annotationStats.textContent =
    `${worldAnnotationMarkers.length}/${worldAnnotationSnapshotMarkers.length} visible` +
    ` · ${summary || 'none'}${sceneDisplay} · scene ${sceneStatus} · gripper ${gripperStatus}`;
}

function fitVisibleAxes() {
  const points = [];
  for (const frame of dynamicAxisFrames) {
    if (!axisIsVisible(frame)) continue;
    points.push(frame.origin, ...frame.axes.map(axis => axis.endpoint));
  }
  for (const marker of worldAnnotationMarkers) {
    points.push(
      marker.center_m.map(value => value - marker.radius_m),
      marker.center_m.map(value => value + marker.radius_m)
    );
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
for (const control of Object.values(annotationVisibilityControls)) {
  control.addEventListener('change', updateAnnotationVisibility);
}
updateAnnotationVisibility();

loadChatSession();
refreshStatus();
if (!focusedUtilityPage) refreshReplayProvenance();
renderCloud();
refreshCloud();
refreshWorldAnnotations();
refreshSpatialAxes();
setInterval(refreshStatus, 2500);
if (!focusedUtilityPage) {
  setInterval(loadChatSession, 3000);
  setInterval(refreshReplayProvenance, 10000);
  setInterval(refreshAuthorization, 1000);
  refreshAuthorization();
}
setInterval(refreshCloud, 300);
setInterval(refreshWorldAnnotations, 1500);
setInterval(refreshSpatialAxes, 1500);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
