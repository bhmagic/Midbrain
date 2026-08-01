from __future__ import annotations

import asyncio
import json
import math
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import Any

from agents import (
    Agent,
    FunctionTool,
    ModelSettings,
    RunConfig,
    RunState,
    Runner,
    ToolExecutionConfig,
    ToolSearchTool,
)

from .basic_safe_home_adapter import BasicSafeHomeAdapter
from .effector_front_adapter import EffectorFrontSkillAdapter
from .gemini_pointing_skill import (
    PointingIdentificationSkill,
    VisualSceneAnalysisSkill,
)
from .integrated_motion_adapter import (
    MAX_CONTROLLED_FRAME_YAW_DELTA_DEG,
    MAX_RELATIVE_NOMINAL_SPEED_M_S,
    IntegratedRelativeMotionAdapter,
)
from .manager_client import ManagerClient
from .phase4_policy import (
    await_with_progress_heartbeat,
    report_operation_progress,
)
from .rgbd_alignment import RgbdAlignmentValidationSkill
from .reviewed_observation_execution import (
    ReviewedObservationExecutionAdapter,
)
from .skill_catalog import discover_agent_skills
from .skill_execution import BoundMethodSkillAdapter, build_agent_tools
from .spatial_registration_adapter import SpatialRegistrationSkillAdapter
from .stationary_calibration_adapter import StationaryCalibrationSkillAdapter
from .tool_registration_adapter import ToolControlFrameSkillAdapter
from .vlm_router import (
    reset_vlm_model_selection,
    set_vlm_model_selection,
)


@dataclass
class InteractiveAgentResult:
    answer: str | None
    state: RunState[Any] | None
    approvals: list[dict[str, Any]]


@dataclass(frozen=True)
class AgentSessionAuthorization:
    auto_authorize_provider_activation: bool = False
    auto_authorize_relative_motion: bool = False
    max_auto_move_cm: float = 5.0
    max_auto_speed_m_s: float = MAX_RELATIVE_NOMINAL_SPEED_M_S
    auto_authorize_stationary_calibration: bool = False
    auto_authorize_stationary_activation: bool = False


def _session_authorization(context_wrapper: Any) -> AgentSessionAuthorization:
    authorization = getattr(context_wrapper, "context", None)
    if isinstance(authorization, AgentSessionAuthorization):
        return authorization
    return AgentSessionAuthorization()


def _runner_context(
    input_value: str | RunState[Any],
    authorization: AgentSessionAuthorization | None,
) -> AgentSessionAuthorization | None:
    """Keep the SDK-owned context when resuming an interrupted run."""

    if isinstance(input_value, RunState):
        return None
    return authorization or AgentSessionAuthorization()


async def provider_activation_needs_approval(
    context_wrapper: Any,
    arguments: dict[str, Any],
    _call_id: str,
) -> bool:
    authorization = _session_authorization(context_wrapper)
    action = str(arguments.get("action") or "").strip().lower()
    return not (
        authorization.auto_authorize_provider_activation
        and action in {"start", "hot", "warm"}
    )


async def relative_motion_needs_approval(
    context_wrapper: Any,
    arguments: dict[str, Any],
    _call_id: str,
) -> bool:
    authorization = _session_authorization(context_wrapper)
    return not (
        authorization.auto_authorize_relative_motion
        and relative_motion_within_authorization(
            arguments,
            max_auto_move_cm=authorization.max_auto_move_cm,
            max_auto_speed_m_s=authorization.max_auto_speed_m_s,
        )
    )


def relative_motion_within_authorization(
    arguments: dict[str, Any],
    *,
    max_auto_move_cm: float,
    max_auto_speed_m_s: float,
) -> bool:
    """Validate one exact translation, rotation, or combined pose preview."""

    try:
        maximum_cm = float(max_auto_move_cm)
        maximum_speed = float(max_auto_speed_m_s)
        distance_m = float(arguments.get("distance_m"))
        planned_speed_m_s = float(
            arguments.get("planned_nominal_speed_m_s")
        )
    except (TypeError, ValueError):
        return False
    if (
        not math.isfinite(maximum_cm)
        or maximum_cm <= 0.0
        or not math.isfinite(maximum_speed)
        or maximum_speed <= 0.0
        or not math.isfinite(distance_m)
        or distance_m < 0.0
        or distance_m * 100.0 > maximum_cm + 1e-9
        or not math.isfinite(planned_speed_m_s)
        or planned_speed_m_s < 0.0
    ):
        return False

    raw_yaw_delta = arguments.get("controlled_frame_yaw_delta_deg")
    if raw_yaw_delta is None:
        yaw_delta_deg = None
    else:
        try:
            yaw_delta_deg = float(raw_yaw_delta)
        except (TypeError, ValueError):
            return False
        if (
            not math.isfinite(yaw_delta_deg)
            or abs(yaw_delta_deg) < 1e-9
            or abs(yaw_delta_deg)
            > MAX_CONTROLLED_FRAME_YAW_DELTA_DEG + 1e-9
        ):
            return False

    motion_intent = str(
        arguments.get("motion_intent") or ""
    ).strip().upper()
    direction = str(arguments.get("direction") or "").strip().upper()
    orientation_policy = str(
        arguments.get("orientation_policy") or ""
    ).strip().upper()
    has_translation = distance_m > 1e-9
    has_rotation = yaw_delta_deg is not None

    if has_translation:
        if (
            direction == "NONE"
            or planned_speed_m_s <= 0.0
            or planned_speed_m_s > maximum_speed + 1e-9
        ):
            return False
    elif (
        direction != "NONE"
        or planned_speed_m_s > 1e-9
        or arguments.get("requested_speed_m_s") is not None
    ):
        return False

    if motion_intent == "NEW_RELATIVE_MOVE":
        return (
            has_translation
            and not has_rotation
            and orientation_policy
            in {
                "POSITION_ONLY",
                "PRESERVE_MEASURED_CONTROLLED_FRAME",
            }
        )
    if motion_intent == "NEW_RELATIVE_POSE_MOVE":
        return (
            has_translation
            and has_rotation
            and orientation_policy
            == "APPLY_CONTROLLED_FRAME_YAW_DELTA"
        )
    if motion_intent == "NEW_RELATIVE_ROTATION":
        return (
            not has_translation
            and has_rotation
            and orientation_policy
            == "APPLY_CONTROLLED_FRAME_YAW_DELTA"
        )
    return False


async def stationary_calibration_needs_approval(
    context_wrapper: Any,
    _arguments: dict[str, Any],
    _call_id: str,
) -> bool:
    authorization = _session_authorization(context_wrapper)
    return not authorization.auto_authorize_stationary_calibration


async def stationary_activation_needs_approval(
    context_wrapper: Any,
    _arguments: dict[str, Any],
    _call_id: str,
) -> bool:
    authorization = _session_authorization(context_wrapper)
    return not authorization.auto_authorize_stationary_activation


def build_turn_safe_session_input(
    history: list[Any],
    new_input: list[Any],
    *,
    limit: int,
) -> list[Any]:
    """Keep recent history without splitting a model/tool reasoning turn."""

    bounded_limit = max(1, int(limit))
    if len(history) <= bounded_limit:
        return [*history, *new_input]

    initial_index = len(history) - bounded_limit
    turn_start_index = 0
    for index in range(initial_index, -1, -1):
        item = history[index]
        if isinstance(item, dict) and item.get("role") == "user":
            turn_start_index = index
            break
    return [*history[turn_start_index:], *new_input]


def build_midbrain_runtime_snapshot(
    providers: list[dict[str, Any]],
    capabilities: list[dict[str, Any]],
    *,
    eligible_skill_tools: list[str],
) -> dict[str, Any]:
    """Preserve complete current Manager evidence and redact credentials."""

    compact_providers: list[dict[str, Any]] = []
    for provider in providers:
        config = provider.get("config")
        config = config if isinstance(config, dict) else {}
        environment = config.get("env")
        environment = environment if isinstance(environment, dict) else {}
        safe_environment = {
            key: (
                "[REDACTED]"
                if any(
                    marker in str(key).strip().upper()
                    for marker in (
                        "API_KEY",
                        "TOKEN",
                        "SECRET",
                        "PASSWORD",
                        "PASSWD",
                        "CREDENTIAL",
                        "COOKIE",
                        "PRIVATE_KEY",
                    )
                )
                else value
            )
            for key, value in environment.items()
        }
        safe_config = {**config, "env": safe_environment}
        compact_providers.append(
            {
                **provider,
                "config": safe_config,
            }
        )
    return {
        "schema": "midbrain.agent_runtime_summary.v1",
        "providers": compact_providers,
        "capabilities": capabilities,
        "eligible_skill_tools": sorted(set(eligible_skill_tools)),
        "omitted": (
            "Only duplicate Skill schemas are omitted. Credential-like "
            "environment values are retained by name but replaced with "
            "[REDACTED]; other environment values remain present."
        ),
    }


class PrototypeAgentDriver:
    def __init__(
        self,
        skill: PointingIdentificationSkill,
        model: str,
        *,
        tool_choice: str = "required",
        workspace_root: Path | None = None,
        eligible_tool_names: set[str] | None = None,
        visual_scene_skill: VisualSceneAnalysisSkill | None = None,
        rgbd_alignment_skill: RgbdAlignmentValidationSkill | None = None,
        spatial_registration_skill: SpatialRegistrationSkillAdapter | None = None,
        effector_front_skill: EffectorFrontSkillAdapter | None = None,
        tool_registration_skill: ToolControlFrameSkillAdapter | None = None,
        stationary_calibration_skill: (
            StationaryCalibrationSkillAdapter | None
        ) = None,
        reviewed_observation_execution_skill: (
            ReviewedObservationExecutionAdapter | None
        ) = None,
        manager: ManagerClient | None = None,
        provider_lifecycle_control: bool = False,
        developer_mode: bool = False,
        integrated_motion_skill: IntegratedRelativeMotionAdapter | None = None,
        basic_safe_home_skill: BasicSafeHomeAdapter | None = None,
        space_cognition_reinitializer: (
            Callable[[str], Awaitable[dict[str, Any]]] | None
        ) = None,
        session: Any | None = None,
        defer_loading: bool = False,
        adapter_timeout_s: float = 60.0,
        stationary_calibration_timeout_s: float = 600.0,
        max_turns: int = 16,
        session_history_item_limit: int | None = None,
    ):
        self.skill = skill
        self.max_turns = int(max_turns)
        if not 1 <= self.max_turns <= 32:
            raise ValueError("max_turns must be between 1 and 32")
        root = workspace_root or Path(__file__).resolve().parents[3]
        eligible = set(
            eligible_tool_names or {"identify_pointed_object"}
        )
        descriptors = discover_agent_skills(root)

        async def identify_adapter(arguments: dict[str, Any]) -> str:
            question = arguments.get("question")
            if not isinstance(question, str) or not question.strip():
                raise ValueError("question must be non-empty text")
            return await self.skill.run(question)

        adapters: dict[str, BoundMethodSkillAdapter] = {
            "test_agent.identify_pointed_object.v1": BoundMethodSkillAdapter(
                identify_adapter
            ),
        }
        if space_cognition_reinitializer is not None:
            async def space_cognition_adapter(
                arguments: dict[str, Any],
            ) -> str:
                reason = arguments.get("reason")
                if not isinstance(reason, str) or not reason.strip():
                    raise ValueError("reason must be non-empty text")
                result = await space_cognition_reinitializer(reason.strip())
                return json.dumps(result, ensure_ascii=False, default=str)

            adapters[
                "skill.initialize_space_cognition.reinitialize.v1"
            ] = BoundMethodSkillAdapter(space_cognition_adapter)
        if visual_scene_skill is not None:
            async def visual_adapter(arguments: dict[str, Any]) -> str:
                question = arguments.get("question")
                if not isinstance(question, str) or not question.strip():
                    raise ValueError("question must be non-empty text")
                return await visual_scene_skill.run(question)

            adapters[
                "test_agent.visual_scene_analysis.v1"
            ] = BoundMethodSkillAdapter(visual_adapter)
        if rgbd_alignment_skill is not None:
            async def rgbd_alignment_adapter(
                arguments: dict[str, Any],
            ) -> str:
                request = arguments.get("request")
                if not isinstance(request, str) or not request.strip():
                    raise ValueError("request must be non-empty text")
                result = await rgbd_alignment_skill.run(request)
                return json.dumps(result, ensure_ascii=False, default=str)

            adapters[
                "test_agent.verify_rgbd_alignment.v1"
            ] = BoundMethodSkillAdapter(rgbd_alignment_adapter)
        if spatial_registration_skill is not None:
            async def spatial_registration_adapter(
                arguments: dict[str, Any],
            ) -> str:
                result = await spatial_registration_skill.run(
                    pixel_yx=arguments.get("pixel_yx"),
                    target_frame=arguments.get("target_frame"),
                    depth_policy=arguments.get("depth_policy"),
                )
                return json.dumps(result, ensure_ascii=False, default=str)

            adapters[
                "skill.spatial_registration_rgbd.v1"
            ] = BoundMethodSkillAdapter(spatial_registration_adapter)
        if effector_front_skill is not None:
            async def effector_front_adapter(
                arguments: dict[str, Any],
            ) -> str:
                result = await effector_front_skill.run(
                    target_frame=arguments.get("target_frame"),
                )
                return json.dumps(result, ensure_ascii=False, default=str)

            adapters[
                "skill.locate_effector_front.v1"
            ] = BoundMethodSkillAdapter(effector_front_adapter)
        if tool_registration_skill is not None:
            async def tool_registration_adapter(
                arguments: dict[str, Any],
            ) -> str:
                result = await tool_registration_skill.run(
                    tool_description=arguments.get("tool_description"),
                    control_frame_purpose=arguments.get(
                        "control_frame_purpose"
                    ),
                    target_frame=arguments.get("target_frame"),
                )
                return json.dumps(result, ensure_ascii=False, default=str)

            adapters[
                "skill.register_tool_to_control_frame.v1"
            ] = BoundMethodSkillAdapter(tool_registration_adapter)
        if stationary_calibration_skill is not None:
            async def stationary_calibration_adapter(
                arguments: dict[str, Any],
            ) -> str:
                result = await stationary_calibration_skill.run(
                    request=arguments.get("request"),
                )
                return json.dumps(result, ensure_ascii=False, default=str)

            adapters[
                "skill.stationary_world_arm_alignment.cli.v1"
            ] = BoundMethodSkillAdapter(stationary_calibration_adapter)
            eligible.add("calibrate_stationary_workcell")
        if reviewed_observation_execution_skill is not None:
            async def reviewed_observation_execution_adapter(
                arguments: dict[str, Any],
            ) -> str:
                decision_id = arguments.get("decision_id")
                if not isinstance(decision_id, str) or not decision_id.strip():
                    raise ValueError("decision_id must be non-empty text")
                result = await reviewed_observation_execution_skill.run(
                    decision_id=decision_id
                )
                return json.dumps(result, ensure_ascii=False, default=str)

            adapters[
                "test_agent.execute_reviewed_observation_motion.v1"
            ] = BoundMethodSkillAdapter(
                reviewed_observation_execution_adapter
            )
        if integrated_motion_skill is not None:
            async def integrated_relative_preview_adapter(
                arguments: dict[str, Any],
            ) -> str:
                result = await integrated_motion_skill.preview(
                    direction=arguments.get("direction"),
                    distance_m=arguments.get("distance_m"),
                    requested_speed_m_s=arguments.get(
                        "requested_speed_m_s"
                    ),
                    reference_frame=arguments.get(
                        "reference_frame", "WORLD"
                    ),
                    arm_mount_assumption=arguments.get(
                        "arm_mount_assumption", "UNKNOWN"
                    ),
                    camera_level_assumption=arguments.get(
                        "camera_level_assumption", "UNKNOWN"
                    ),
                    fixed_vio_rig_assumption=arguments.get(
                        "fixed_vio_rig_assumption", "UNKNOWN"
                    ),
                    orientation_policy=arguments.get(
                        "orientation_policy", "POSITION_ONLY"
                    ),
                    controlled_frame_yaw_delta_deg=arguments.get(
                        "controlled_frame_yaw_delta_deg"
                    ),
                )
                return json.dumps(result, ensure_ascii=False, default=str)

            adapters[
                "skill.integrated_relative_effector_motion.preview.v1"
            ] = BoundMethodSkillAdapter(
                integrated_relative_preview_adapter
            )
            eligible.add("preview_relative_effector_motion")

        tools = build_agent_tools(
            descriptors,
            adapters,
            eligible_tool_names=eligible,
            defer_loading=defer_loading,
            adapter_timeout_s=adapter_timeout_s,
            adapter_timeout_overrides_s=(
                {
                    "calibrate_stationary_workcell": float(
                        stationary_calibration_timeout_s
                    )
                }
                if stationary_calibration_skill is not None
                else None
            ),
            approval_overrides=(
                {
                    "calibrate_stationary_workcell": (
                        stationary_calibration_needs_approval
                    )
                }
                if stationary_calibration_skill is not None
                else None
            ),
        )
        self.offered_skill_descriptors = [
            descriptor
            for descriptor in descriptors
            if descriptor.tool_name in eligible
        ]

        offered_tools = list(tools)
        if defer_loading:
            offered_tools.append(ToolSearchTool())
        if stationary_calibration_skill is not None:
            async def review_and_activate_stationary_calibration(
                _context,
                raw_arguments: str,
            ) -> str:
                arguments = json.loads(raw_arguments)
                result = await stationary_calibration_skill.review_and_activate(
                    alignment_id=arguments.get("alignment_id"),
                    candidate_sha256=arguments.get("candidate_sha256"),
                )
                return json.dumps(result, ensure_ascii=False, default=str)

            offered_tools.append(
                FunctionTool(
                    name="review_and_activate_stationary_calibration",
                    description=(
                        "Review and activate one exact persisted stationary "
                        "world-to-arm calibration candidate. Copy alignment_id "
                        "and candidate_sha256 unchanged only from the current "
                        "run's calibration tool required_next_tool; never "
                        "replay activation arguments from an earlier user turn "
                        "or service boot. This submits no arm motion. "
                        "Manager independently revalidates candidate quality, "
                        "provenance, current VIO tracking, and expiration, then "
                        "publishes a motion-usable transform for at most five "
                        "minutes."
                    ),
                    params_json_schema={
                        "type": "object",
                        "properties": {
                            "alignment_id": {
                                "type": "string",
                                "pattern": "^[0-9A-Za-z-]+$",
                            },
                            "candidate_sha256": {
                                "type": "string",
                                "pattern": "^[0-9a-f]{64}$",
                            },
                        },
                        "required": [
                            "alignment_id",
                            "candidate_sha256",
                        ],
                        "additionalProperties": False,
                    },
                    on_invoke_tool=(
                        review_and_activate_stationary_calibration
                    ),
                    strict_json_schema=True,
                    needs_approval=stationary_activation_needs_approval,
                )
            )
        lifecycle_control = developer_mode or provider_lifecycle_control
        if lifecycle_control:
            if manager is None:
                raise ValueError(
                    "Provider lifecycle control requires a Manager client"
                )

            async def inspect_runtime(_context, raw_arguments: str) -> str:
                arguments = json.loads(raw_arguments)
                if arguments != {}:
                    raise ValueError(
                        "inspect_midbrain_runtime does not accept arguments"
                    )
                providers, capabilities = await asyncio.gather(
                    manager.providers(),
                    manager.capabilities(),
                )
                return json.dumps(
                    build_midbrain_runtime_snapshot(
                        providers,
                        capabilities,
                        eligible_skill_tools=[
                            descriptor.tool_name
                            for descriptor in self.offered_skill_descriptors
                        ],
                    ),
                    ensure_ascii=False,
                    default=str,
                )

            async def set_provider_residency(
                _context,
                raw_arguments: str,
            ) -> str:
                arguments = json.loads(raw_arguments)
                provider_id = arguments.get("provider_id")
                action = arguments.get("action")
                if not isinstance(provider_id, str) or not provider_id.strip():
                    raise ValueError("provider_id must be non-empty text")
                if action not in {"start", "hot", "warm", "stop"}:
                    raise ValueError(
                        "action must be start, hot, warm, or stop"
                    )
                configured = {
                    str(provider.get("config", {}).get("id"))
                    for provider in await manager.providers()
                }
                if provider_id not in configured:
                    raise ValueError(
                        f"{provider_id} is not a configured Provider"
                    )
                result = await manager.set_residency(provider_id, action)
                return json.dumps(
                    {
                        "lifecycle_request_complete": True,
                        "provider_id": provider_id,
                        "requested_action": action.upper(),
                        "manager_result": result,
                        "agent_instruction": (
                            "Do not request this identical lifecycle "
                            "transition again in the current run. Continue "
                            "the original finite task; its adapter performs "
                            "its own bounded readiness checks."
                        ),
                    },
                    ensure_ascii=False,
                    default=str,
                )

            offered_tools.extend(
                [
                    FunctionTool(
                        name="inspect_midbrain_runtime",
                        description=(
                            "Inspect the complete current Manager evidence for "
                            "configured Midbrain Providers, including Provider "
                            "reports, controller telemetry, command and target "
                            "state, launch configuration, identities, "
                            "timestamps, capability availability, and eligible "
                            "finite Skill names. Only duplicate Skill schemas "
                            "are omitted; credential-like environment values "
                            "are redacted. This operation is read-only."
                        ),
                        params_json_schema={
                            "type": "object",
                            "properties": {},
                            "required": [],
                            "additionalProperties": False,
                        },
                        on_invoke_tool=inspect_runtime,
                        strict_json_schema=True,
                    ),
                    FunctionTool(
                        name="set_provider_residency",
                        description=(
                            "Request one configured Provider lifecycle "
                            "transition through Manager. Use when the "
                            "user explicitly requests a lifecycle change "
                            "or when a Provider is a necessary cold dependency "
                            "of the requested task. Call this tool directly "
                            "instead of asking for conversational permission; "
                            "the host approval boundary handles the decision "
                            "and may apply active browser-session "
                            "authorization. Use the exact Provider ID."
                        ),
                        params_json_schema={
                            "type": "object",
                            "properties": {
                                "provider_id": {
                                    "type": "string",
                                    "description": (
                                        "Exact configured Provider ID from "
                                        "inspect_midbrain_runtime."
                                    ),
                                },
                                "action": {
                                    "type": "string",
                                    "enum": ["start", "hot", "warm", "stop"],
                                },
                            },
                            "required": ["provider_id", "action"],
                            "additionalProperties": False,
                        },
                        on_invoke_tool=set_provider_residency,
                        strict_json_schema=True,
                        needs_approval=provider_activation_needs_approval,
                    ),
                ]
            )
        if integrated_motion_skill is not None:
            async def execute_integrated_motion_preview(
                _context,
                raw_arguments: str,
            ) -> str:
                arguments = json.loads(raw_arguments)
                result = await integrated_motion_skill.execute(
                    preview_id=arguments.get("preview_id"),
                    motion_intent=arguments.get("motion_intent"),
                    direction=arguments.get("direction"),
                    reference_frame=arguments.get("reference_frame"),
                    resolved_direction_arm_base=arguments.get(
                        "resolved_direction_arm_base"
                    ),
                    distance_m=arguments.get("distance_m"),
                    original_request_distance_m=arguments.get(
                        "original_request_distance_m"
                    ),
                    requested_speed_m_s=arguments.get(
                        "requested_speed_m_s"
                    ),
                    requested_duration_s=arguments.get(
                        "requested_duration_s"
                    ),
                    planned_duration_s=arguments.get(
                        "planned_duration_s"
                    ),
                    planned_nominal_speed_m_s=arguments.get(
                        "planned_nominal_speed_m_s"
                    ),
                    timing_safety_limited=arguments.get(
                        "timing_safety_limited"
                    ),
                    target_position_m=arguments.get("target_position_m"),
                    orientation_policy=arguments.get(
                        "orientation_policy", "POSITION_ONLY"
                    ),
                    target_orientation_rpy_rad=arguments.get(
                        "target_orientation_rpy_rad"
                    ),
                    controlled_frame_yaw_delta_deg=arguments.get(
                        "controlled_frame_yaw_delta_deg"
                    ),
                )
                return json.dumps(result, ensure_ascii=False, default=str)

            offered_tools.extend(
                [
                    FunctionTool(
                        name="execute_integrated_motion_preview",
                        description=(
                            "Execute one exact, fresh Integrated Controller "
                            "relative translation, controlled-frame yaw, or "
                            "combined pose preview. Copy all arguments "
                            "exactly from preview_relative_effector_motion. "
                            "Host authorization, whether manual or an active "
                            "bounded session policy, immediately sends the "
                            "existing one-shot commit trigger, requests "
                            "physical arm motion, and waits for the "
                            "controller's bounded completion result."
                        ),
                        params_json_schema={
                            "type": "object",
                            "properties": {
                                "preview_id": {"type": "string"},
                                "motion_intent": {
                                    "type": "string",
                                    "enum": [
                                        "NEW_RELATIVE_MOVE",
                                        "NEW_RELATIVE_POSE_MOVE",
                                        "NEW_RELATIVE_ROTATION",
                                    ],
                                },
                                "direction": {
                                    "type": "string",
                                    "enum": [
                                        "NONE",
                                        "UP",
                                        "DOWN",
                                        "FRONT",
                                        "BACK",
                                        "LEFT",
                                        "RIGHT",
                                        "NORTH",
                                        "SOUTH",
                                        "EAST",
                                        "WEST",
                                        "POSITIVE_X",
                                        "NEGATIVE_X",
                                        "POSITIVE_Y",
                                        "NEGATIVE_Y",
                                        "POSITIVE_Z",
                                        "NEGATIVE_Z",
                                        "ARM_BASE_POSITIVE_X",
                                        "ARM_BASE_NEGATIVE_X",
                                        "ARM_BASE_POSITIVE_Y",
                                        "ARM_BASE_NEGATIVE_Y",
                                        "ARM_BASE_POSITIVE_Z",
                                        "ARM_BASE_NEGATIVE_Z",
                                    ],
                                },
                                "reference_frame": {
                                    "type": "string",
                                    "enum": [
                                        "WORLD",
                                        "CAMERA_LEVEL",
                                        "ARM_BASE",
                                        "CONTROLLED_FRAME",
                                    ],
                                },
                                "resolved_direction_arm_base": {
                                    "type": "array",
                                    "items": {"type": "number"},
                                    "minItems": 3,
                                    "maxItems": 3,
                                },
                                "distance_m": {
                                    "type": "number",
                                    "minimum": 0.0,
                                    "maximum": 0.2,
                                },
                                "original_request_distance_m": {
                                    "type": "number",
                                    "minimum": 0.0,
                                    "maximum": 0.2,
                                },
                                "requested_speed_m_s": {
                                    "anyOf": [
                                        {
                                            "type": "number",
                                            "exclusiveMinimum": 0.0,
                                            "maximum": (
                                                MAX_RELATIVE_NOMINAL_SPEED_M_S
                                            ),
                                        },
                                        {"type": "null"},
                                    ],
                                },
                                "requested_duration_s": {
                                    "type": "number",
                                    "minimum": 0.25,
                                    "maximum": 8.0,
                                },
                                "planned_duration_s": {
                                    "type": "number",
                                    "minimum": 0.25,
                                    "maximum": 8.0,
                                },
                                "planned_nominal_speed_m_s": {
                                    "type": "number",
                                    "minimum": 0.0,
                                    "maximum": (
                                        MAX_RELATIVE_NOMINAL_SPEED_M_S
                                    ),
                                },
                                "timing_safety_limited": {
                                    "type": "boolean",
                                },
                                "target_position_m": {
                                    "type": "array",
                                    "items": {"type": "number"},
                                    "minItems": 3,
                                    "maxItems": 3,
                                },
                                "orientation_policy": {
                                    "type": "string",
                                    "enum": [
                                        "POSITION_ONLY",
                                        (
                                            "PRESERVE_MEASURED_"
                                            "CONTROLLED_FRAME"
                                        ),
                                        (
                                            "APPLY_CONTROLLED_FRAME_"
                                            "YAW_DELTA"
                                        ),
                                    ],
                                },
                                "controlled_frame_yaw_delta_deg": {
                                    "anyOf": [
                                        {
                                            "type": "number",
                                            "minimum": (
                                                -MAX_CONTROLLED_FRAME_YAW_DELTA_DEG
                                            ),
                                            "maximum": (
                                                MAX_CONTROLLED_FRAME_YAW_DELTA_DEG
                                            ),
                                        },
                                        {"type": "null"},
                                    ],
                                },
                                "target_orientation_rpy_rad": {
                                    "anyOf": [
                                        {
                                            "type": "array",
                                            "items": {"type": "number"},
                                            "minItems": 3,
                                            "maxItems": 3,
                                        },
                                        {"type": "null"},
                                    ],
                                },
                            },
                            "required": [
                                "preview_id",
                                "motion_intent",
                                "direction",
                                "reference_frame",
                                "resolved_direction_arm_base",
                                "distance_m",
                                "original_request_distance_m",
                                "requested_speed_m_s",
                                "requested_duration_s",
                                "planned_duration_s",
                                "planned_nominal_speed_m_s",
                                "timing_safety_limited",
                                "target_position_m",
                                "orientation_policy",
                                "controlled_frame_yaw_delta_deg",
                                "target_orientation_rpy_rad",
                            ],
                            "additionalProperties": False,
                        },
                        on_invoke_tool=execute_integrated_motion_preview,
                        strict_json_schema=True,
                        needs_approval=relative_motion_needs_approval,
                    ),
                ]
            )
        if basic_safe_home_skill is not None:
            async def execute_basic_safe_home(
                _context,
                _raw_arguments: str,
            ) -> str:
                result = await basic_safe_home_skill.execute()
                return json.dumps(result, ensure_ascii=False, default=str)

            offered_tools.append(
                FunctionTool(
                    name="execute_basic_safe_home",
                    description=(
                        "Run the Basic Controller's configured safe-home "
                        "operation. It preempts active arm control, moves the "
                        "six arm joints to configured home while preserving "
                        "the measured gripper angle, and returns to gravity "
                        "float. It always requires human approval and reports "
                        "only controller-confirmed completion."
                    ),
                    params_json_schema={
                        "type": "object",
                        "properties": {},
                        "required": [],
                        "additionalProperties": False,
                    },
                    on_invoke_tool=execute_basic_safe_home,
                    strict_json_schema=True,
                    needs_approval=True,
                )
            )
        narrow_initial = eligible == {"identify_pointed_object"}
        reviewed_execution_only = eligible == {
            "execute_reviewed_observation_motion"
        }
        if developer_mode:
            instructions = (
                "This is the Midbrain developer agent. You may inspect the "
                "configured Provider and Skill catalog and invoke only the "
                "typed tools offered by this host. Use "
                "inspect_midbrain_runtime before choosing a Provider ID. "
                "Its snapshot includes complete current Provider reports, "
                "controller telemetry, command and target state, launch "
                "configuration, identities, capabilities, and timestamps; "
                "only duplicate Skill schemas are omitted, while "
                "credential-like environment values are redacted. "
                "When a Provider lifecycle transition is necessary, call "
                "set_provider_residency immediately. Never answer by asking "
                "for conversational permission: the tool interruption is the "
                "approval request, and the host may resolve it from active "
                "browser-session authorization. A rejected lifecycle "
                "interruption is final for the current run: do not request "
                "the identical transition again. After an approved lifecycle "
                "call, do not request that identical transition again in the "
                "same run; inspect once and report nonconvergence if the "
                "runtime still does not satisfy the dependency. For a "
                "requested relative "
                "effector motion, inspect the current runtime even if earlier "
                "conversation says the Providers were running, then activate "
                "robot_arm.rebot_dm to HOT first, then activate "
                "robot_arm.primary.integrated to HOT. Only after both "
                "dependencies are ready, create the nonphysical preview and "
                "then "
                "request execution of that exact preview; do not stop at "
                "asking the user to construct a separate decision ID. The "
                "execution tool itself provides the human approval boundary, "
                "and approval immediately sends its one-shot commit trigger. "
                "A PREVIEW_READY tool result is an incomplete workflow: call "
                "its required_next_tool immediately with unchanged arguments "
                "instead of answering the user. "
                "If preview reports DEPENDENCY_UNAVAILABLE, follow its "
                "required_next_tool and activation sequence; do not repeat the "
                "same preview call while the controller is unreachable. "
                "Do not treat target-edit engagement as completed motion. "
                "Report success only when the tool returns "
                "physical_motion_completed=true; otherwise report its exact "
                "unsuccessful or unconfirmed completion outcome. "
                "Treat every relative motion request as a new displacement "
                "from the current measured pose, including repeated requests. "
                "This relative-pose Skill always uses the Integrated "
                "Controller's PRESS_MIT one-shot path, so an explicit MIT-mode "
                "request is already satisfied and needs no additional mode "
                "field. For a controlled-frame head turn, use "
                "orientation_policy=APPLY_CONTROLLED_FRAME_YAW_DELTA and "
                "controlled_frame_yaw_delta_deg. The controlled frame is +X "
                "forward, +Y left, +Z up: positive yaw turns left and negative "
                "yaw turns right. Pure rotation uses direction=NONE, "
                "distance_m=0, and requested_speed_m_s=null; a simultaneous "
                "translation keeps its actual direction and distance. The "
                "phrase arm forward explicitly maps to "
                "direction=ARM_BASE_POSITIVE_X with reference_frame=ARM_BASE. "
                "Never "
                "reject a combined translation and bounded head turn merely "
                "because the older preservation-only policy could not express "
                "it. "
                "Skill "
                "adapters retain their deterministic policy and authority "
                "gates. Never translate a prompt into an arbitrary "
                "Provider HTTP path, fabricate authority, bypass a Skill "
                "adapter, or claim that physical action succeeded without "
                "the exact tool result. Preserve Manager binding, fencing, "
                "authorization, and Provider-side safety decisions."
            )
        elif reviewed_execution_only:
            instructions = (
                "This is a decision-specific physical execution boundary for "
                "a physical-agent platform. Call exactly the one eligible "
                "finite skill with the exact decision ID supplied by the host. "
                "Do not create or alter coordinates, speeds, modes, contact "
                "permissions, leases, safe-home actions, or fallback motion. "
                "Report only the tool's actual controller result; never infer "
                "that motion succeeded."
            )
        else:
            instructions = (
                "This is a deliberately narrow initial agent surface for a "
                "physical-agent platform. "
                if narrow_initial
                else "This is the regular Midbrain finite-Skill agent surface. "
            )
            instructions += (
                "For a finite task, select one Skill from its name and "
                "description; do not infer a Provider from brand names. The "
                "eligible Skill allowlist is "
                f"{', '.join(sorted(eligible))}. Pass the user's complete "
                "request to the selected Skill. Treat its JSON as the complete "
                "sensor evidence, explain the result plainly, and mention "
                "uncertainty. Never invent depth, coordinates, motion, "
                "contact, or additional sensor evidence, and never claim "
                "that the robot moved or interacted with an object unless an "
                "approved typed tool reports that exact result."
            )
            if lifecycle_control:
                instructions += (
                    " For an explicit Provider lifecycle request, or when a "
                    "cold Provider is required for the requested Skill, "
                    "inspect the runtime and call set_provider_residency for "
                    "the necessary transition instead of answering with a "
                    "permission request or calling an unrelated Skill. The "
                    "runtime snapshot includes complete current Provider "
                    "reports, controller telemetry, command and target state, "
                    "launch configuration, identities, capabilities, and "
                    "timestamps. Only duplicate Skill schemas are omitted, "
                    "while credential-like environment values are redacted. "
                    "Every lifecycle call remains authorization-gated by its "
                    "tool policy; an eligible active session policy can "
                    "authorize it without an SDK interruption. Never ask for "
                    "a separate conversational approval. A rejected "
                    "lifecycle interruption is final for the current run, "
                    "and an identical approved transition must not be "
                    "requested a second time in that run. Do not "
                    "claim activation until the Manager tool returns success. "
                    "After an approved activation, continue the original task "
                    "within the same run."
                )
            if integrated_motion_skill is not None:
                instructions += (
                    " For a requested relative end-effector motion, inspect "
                    "the current runtime even if earlier conversation says the "
                    "Providers were running. Activate robot_arm.rebot_dm to "
                    "HOT first, then "
                    "activate robot_arm.primary.integrated to HOT. Only after "
                    "both are ready, create the nonphysical Integrated IK "
                    "preview and then request "
                    "execution of that exact preview. "
                    "If either Provider is stopped, cold, or requires HOT "
                    "recovery, do not report that approval is needed and end "
                    "the run: call set_provider_residency immediately so its "
                    "actual approval interruption can be resolved. "
                    "If manual authorization is required, tell the operator "
                    "that approval immediately sends its one-shot commit "
                    "trigger. "
                    "A PREVIEW_READY result is incomplete: call its "
                    "required_next_tool immediately with unchanged arguments "
                    "instead of answering the user. "
                    "If preview reports DEPENDENCY_UNAVAILABLE, follow its "
                    "required_next_tool and activation sequence; do not repeat "
                    "the same preview call while the controller is unreachable. "
                    "If preview reports INTEGRATED_RECOVERY_REQUIRED, call "
                    "its required_next_tool unchanged and request the explicit "
                    "approved HOT transition. Do this even when Manager says "
                    "the provider process is already running: HOT is the "
                    "controller recovery boundary that reacquires its Basic "
                    "lease. After approval, create a fresh preview. "
                    "If preview asks ARM_MOUNT_CONFIRMATION_REQUIRED, ask its "
                    "exact y/n question. On yes retry the same request with "
                    "arm_mount_assumption=CONFIRMED_X_FORWARD_Z_UP; on no "
                    "retry with arm_mount_assumption=REJECTED_OR_UNKNOWN. "
                    "A current reviewed world-to-arm transform has priority "
                    "for every WORLD direction, including ordinary up/down/"
                    "front/back/left/right. The upright-mount assumption is "
                    "only a fallback when measured resolution is unavailable. "
                    "Do not ask the mount question after a reviewed active "
                    "transform resolves the requested direction. An "
                    "explicit world +X/-X/+Y/-Y/+Z/-Z request must use the "
                    "matching POSITIVE_*/NEGATIVE_* direction and a reviewed "
                    "motion-usable world-to-arm transform; never ask for or "
                    "apply the upright-mount fallback to that request. If a "
                    "preview reports WORLD_TO_ARM_ALIGNMENT_REQUIRED, call "
                    "its required_next_tool unchanged. After exact candidate "
                    "activation returns motion_usable=true, retry the original "
                    "world-axis preview; do not ask about the upright mount. "
                    "If the "
                    "operator asks to keep the effector head, "
                    "pointing direction, attitude, or 3D orientation during "
                    "translation, set orientation_policy="
                    "PRESERVE_MEASURED_CONTROLLED_FRAME. This uses the "
                    "Integrated Controller's measured controlled-frame "
                    "orientation with POSE_6DOF; do not describe it as "
                    "position-only IK. If the operator asks to rotate the "
                    "effector/hand/head direction to its own right or left, "
                    "set orientation_policy="
                    "APPLY_CONTROLLED_FRAME_YAW_DELTA and pass the signed "
                    "controlled_frame_yaw_delta_deg. Controlled-frame +X is "
                    "forward, +Y is left, and +Z is up, so left is positive "
                    "yaw and right is negative yaw. For rotation without "
                    "translation, use direction=NONE, distance_m=0, and no "
                    "requested speed. For a combined translation and turn, "
                    "keep the requested translation fields and add the yaw "
                    "delta. Explicit arm forward maps to "
                    "direction=ARM_BASE_POSITIVE_X and "
                    "reference_frame=ARM_BASE. This Skill always stages "
                    "PRESS_MIT ONE_SHOT, so "
                    "an explicit MIT-mode request is supported without a "
                    "separate mode argument. When the operator specifies a motion "
                    "speed, pass it as requested_speed_m_s. The adapter "
                    "converts distance/speed into a requested trajectory "
                    "duration. Describe it as nominal average endpoint speed, "
                    "not constant Cartesian velocity, and report any longer "
                    "Provider-planned duration as safety limiting. Do not "
                    "claim the requested speed was achieved unless the result "
                    "supports that statement. If preview asks "
                    "FIXED_VIO_RIG_CONFIRMATION_REQUIRED, ask its exact y/n "
                    "question. On yes retry with "
                    "fixed_vio_rig_assumption="
                    "CONFIRMED_FIXED_STATIONARY_RIG so the non-destructive "
                    "tracking check can run when measured world alignment is "
                    "needed; on no retry with REJECTED_OR_UNKNOWN and do not "
                    "imply that visual verification is available. When the "
                    "measured transform or upright fallback already defines "
                    "the requested direction, optional image or exact-depth "
                    "evidence "
                    "must not prevent creation of the IK preview. Report "
                    "BEFORE_EVIDENCE_UNAVAILABLE or skipped visual evidence "
                    "separately. Never use the "
                    "destructive reinitialize tool as a readiness probe. "
                    "Do not claim motion from the target-edit engagement "
                    "response; report success only when the execution tool "
                    "returns physical_motion_completed=true. Otherwise report "
                    "the controller's completion outcome as an unsuccessful "
                    "or unconfirmed move. Treat every relative motion request "
                    "as a new displacement from the current measured pose, "
                    "including repeated requests. For an available visual "
                    "check, distinguish controller completion from the "
                    "gravity-aligned before/after visual verdict. Missing "
                    "exact depth makes optional visual verification "
                    "unavailable; it does not make controller completion "
                    "unsuccessful. If a preview returns "
                    "IK_PREVIEW_REJECTED, report the resolved arm-base "
                    "direction, start/target pose, per-joint endpoint travel, "
                    "and configured endpoint limits. Do not infer an axis "
                    "mistake from joint travel alone."
                )
            if stationary_calibration_skill is not None:
                instructions += (
                    " When the operator asks to establish, calibrate, or "
                    "validate the world-to-arm-base relationship, use "
                    "calibrate_stationary_workcell immediately. Do not ask for "
                    "conversational permission first; the tool authorization "
                    "boundary may be satisfied before execution by active "
                    "browser-session calibration authorization, otherwise it "
                    "creates an SDK interruption. VIO "
                    "establishes its local "
                    "world epoch, while this Skill observes the stationary "
                    "robot base and publishes the world-to-arm-base transform; "
                    "do not claim that VIO alone measures the arm-base "
                    "extrinsic. A calibration candidate is incomplete: call "
                    "its required_next_tool immediately with unchanged exact "
                    "alignment ID and digest. Report the relationship as "
                    "established only after that tool returns "
                    "motion_usable=true. Candidate review and bounded "
                    "activation have their own authorization policy and "
                    "Manager revalidates all safety gates. "
                    "If activation returns FRESH_CALIBRATION_REQUIRED, never "
                    "retry that alignment. When the current user request is "
                    "to establish the relationship, call "
                    "calibrate_stationary_workcell again with the current "
                    "request and activate only its new candidate. "
                    "The Skill acquires "
                    "global motion inhibit, so "
                    "a later Integrated motion requires a fresh explicit "
                    "approved HOT transition before preview."
                )
        if basic_safe_home_skill is not None:
            instructions += (
                " For an explicit safe-home request, use "
                "execute_basic_safe_home after ensuring the Basic Provider "
                "is running. Safe-home preempts active arm control and always "
                "requires approval. Do not substitute gravity float, Provider "
                "stop, or healthy status for homing. Report completion only "
                "when physical_motion_completed=true. Safe-home preempts "
                "Integrated's Basic lease, so RECOVERY_REQUIRED afterward is "
                "expected. On the next Integrated motion request, perform the "
                "explicit approved Integrated HOT recovery and continue with "
                "a fresh preview instead of treating recovery as a terminal "
                "failure."
            )
        if space_cognition_reinitializer is not None:
            instructions += (
                " Use reinitialize_space_cognition only when the operator "
                "explicitly requests a new local origin or accepts recovery "
                "from spatial drift. It is not a readiness probe. Approval "
                "revokes active workcell calibrations, resets the VIO epoch, "
                "and clears observations bound to the old epoch. After it "
                "succeeds, any world-to-arm calibration required by a later "
                "motion task must be established again."
            )
        self.agent = Agent(
            name="Physical Agent Prototype Driver",
            model=model,
            instructions=instructions,
            model_settings=ModelSettings(
                tool_choice=tool_choice,
                parallel_tool_calls=False,
            ),
            tools=offered_tools,
        )
        self.run_config = RunConfig(
            workflow_name="Midbrain Phase 4 finite Skill selection",
            session_input_callback=(
                partial(
                    build_turn_safe_session_input,
                    limit=session_history_item_limit,
                )
                if session_history_item_limit is not None
                else None
            ),
            tool_execution=ToolExecutionConfig(
                max_function_tool_concurrency=1,
                pre_approval_tool_input_guardrails=True,
            ),
            tool_not_found_behavior="return_error_to_model",
        )
        self.session = session

    async def run(self, prompt: str) -> str:
        result = await self._run(prompt)
        if result.interruptions:
            raise RuntimeError(
                "This tool call requires approval on the developer agent surface"
            )
        return self._final_output(result.final_output)

    async def run_interactive(
        self,
        input_value: str | RunState[Any],
        *,
        model_override: str | None = None,
        reasoning_effort: str | None = None,
        vlm_model_override: str | None = None,
        authorization: AgentSessionAuthorization | None = None,
    ) -> InteractiveAgentResult:
        result = await self._run(
            input_value,
            model_override=model_override,
            reasoning_effort=reasoning_effort,
            vlm_model_override=vlm_model_override,
            authorization=authorization,
        )
        if result.interruptions:
            return InteractiveAgentResult(
                answer=None,
                state=result.to_state(),
                approvals=[
                    self._approval_description(item)
                    for item in result.interruptions
                ],
            )
        return InteractiveAgentResult(
            answer=self._final_output(result.final_output),
            state=None,
            approvals=[],
        )

    async def _run(
        self,
        input_value: str | RunState[Any],
        *,
        model_override: str | None = None,
        reasoning_effort: str | None = None,
        vlm_model_override: str | None = None,
        authorization: AgentSessionAuthorization | None = None,
    ):
        if not os.getenv("OPENAI_API_KEY", "").strip():
            raise RuntimeError("OPENAI_API_KEY is empty in config/api_keys.env")
        report_operation_progress("AGENT_MODEL_SELECTION")
        run_config = self.run_config
        if model_override is not None or reasoning_effort is not None:
            run_config = replace(
                self.run_config,
                model=model_override,
                model_settings=(
                    None
                    if reasoning_effort is None
                    else ModelSettings(
                        reasoning={"effort": reasoning_effort}
                    )
                ),
            )
        vlm_token = set_vlm_model_selection(vlm_model_override)
        try:
            result = await await_with_progress_heartbeat(
                Runner.run(
                    self.agent,
                    input_value,
                    context=_runner_context(input_value, authorization),
                    max_turns=self.max_turns,
                    run_config=run_config,
                    session=self.session,
                ),
                stage="AGENT_MODEL_AWAITING_RESPONSE",
            )
        finally:
            reset_vlm_model_selection(vlm_token)
        report_operation_progress("AGENT_RUN_COMPLETED")
        return result

    @staticmethod
    def _final_output(output: Any) -> str:
        if isinstance(output, str):
            return output
        return json.dumps(output, ensure_ascii=False, default=str)

    @staticmethod
    def _approval_description(item: Any) -> dict[str, Any]:
        raw_item = item.raw_item
        if hasattr(raw_item, "model_dump"):
            raw = raw_item.model_dump(mode="json")
        elif isinstance(raw_item, dict):
            raw = dict(raw_item)
        else:
            raw = {
                key: getattr(raw_item, key)
                for key in ("call_id", "name", "arguments")
                if hasattr(raw_item, key)
            }
        arguments: dict[str, Any] = {}
        raw_arguments = raw.get("arguments", {})
        if isinstance(raw_arguments, str):
            try:
                decoded = json.loads(raw_arguments)
                if isinstance(decoded, dict):
                    arguments = decoded
            except json.JSONDecodeError:
                arguments = {}
        elif isinstance(raw_arguments, dict):
            arguments = raw_arguments

        tool_name = item.tool_name
        title = f"Approve {tool_name}?"
        summary = (
            "The Agent is requesting permission to run this protected "
            "operation."
        )
        warning = "The operation will run only if you approve it."
        confirm_label = "Approve operation"
        details: list[dict[str, str]] = []
        if tool_name == "set_provider_residency":
            provider_id = str(arguments.get("provider_id") or "unknown")
            action = str(arguments.get("action") or "unknown").upper()
            action_text = {
                "START": "start",
                "HOT": "start and activate",
                "WARM": "place in warm standby",
                "STOP": "stop",
            }.get(action, action.lower())
            title = f"{action_text.capitalize()} {provider_id}?"
            summary = (
                f"The Agent needs to {action_text} Provider {provider_id} "
                "through Midbrain Manager."
            )
            warning = (
                "This may initialize attached hardware or change its powered "
                "state. Provider-side safety checks remain active."
            )
            confirm_label = f"Approve {action_text}"
            details = [
                {"label": "Provider", "value": provider_id},
                {"label": "Requested state", "value": action},
            ]
        elif tool_name == "execute_integrated_motion_preview":
            direction = str(arguments.get("direction") or "unknown").upper()
            motion_intent = str(
                arguments.get("motion_intent") or "NEW_RELATIVE_MOVE"
            ).upper()
            try:
                distance_cm = float(arguments.get("distance_m")) * 100.0
                distance_text = f"{distance_cm:g} cm"
            except (TypeError, ValueError):
                distance_text = "unknown distance"
            target = arguments.get("target_position_m")
            target_text = (
                ", ".join(f"{float(value):.4f}" for value in target) + " m"
                if isinstance(target, list) and len(target) == 3
                else "unknown"
            )
            orientation_policy = str(
                arguments.get("orientation_policy") or "POSITION_ONLY"
            ).upper()
            raw_yaw_delta = arguments.get(
                "controlled_frame_yaw_delta_deg"
            )
            try:
                yaw_delta_deg = (
                    None
                    if raw_yaw_delta is None
                    else float(raw_yaw_delta)
                )
            except (TypeError, ValueError):
                yaw_delta_deg = None
            yaw_text = "none"
            if yaw_delta_deg is not None and math.isfinite(yaw_delta_deg):
                turn = "left" if yaw_delta_deg > 0.0 else "right"
                yaw_text = (
                    f"{abs(yaw_delta_deg):g} degrees {turn} "
                    f"(signed yaw {yaw_delta_deg:g} degrees)"
                )
            requested_speed = arguments.get("requested_speed_m_s")
            requested_speed_text = (
                "not applicable to rotation-only motion"
                if motion_intent == "NEW_RELATIVE_ROTATION"
                else "default 3-second trajectory"
                if requested_speed is None
                else f"{float(requested_speed):g} m/s nominal average"
            )
            try:
                planned_duration_text = (
                    f"{float(arguments.get('planned_duration_s')):g} s"
                )
            except (TypeError, ValueError):
                planned_duration_text = "unknown"
            if motion_intent == "NEW_RELATIVE_ROTATION":
                title = f"Rotate the effector head {yaw_text}?"
            elif motion_intent == "NEW_RELATIVE_POSE_MOVE":
                title = (
                    f"Move the arm {direction} by {distance_text} and "
                    f"rotate the head {yaw_text}?"
                )
            else:
                title = f"Move the arm {direction} by {distance_text}?"
            summary = (
                "The Integrated Controller has already produced a valid "
                "nonphysical IK preview. Approval requests execution of "
                "that exact staged preview."
            )
            warning = (
                "This is physical arm motion. Keep clear of the arm and be "
                "ready to use the emergency stop. Approval immediately sends "
                "the MIT one-shot commit; no separate controller-button press "
                "is required. Optional visual verification may be skipped, "
                "unavailable, or inconclusive; it does not verify the "
                "commanded yaw angle."
            )
            confirm_label = "Approve arm motion"
            details = [
                {"label": "Direction", "value": direction},
                {"label": "Distance", "value": distance_text},
                {
                    "label": "Requested speed",
                    "value": requested_speed_text,
                },
                {
                    "label": "Planned duration",
                    "value": planned_duration_text,
                },
                {"label": "Intent", "value": motion_intent},
                {"label": "Target XYZ", "value": target_text},
                {"label": "Controlled-frame yaw", "value": yaw_text},
                {
                    "label": "Orientation",
                    "value": (
                        "Preserve measured controlled-frame 3D orientation"
                        if orientation_policy
                        == "PRESERVE_MEASURED_CONTROLLED_FRAME"
                        else "Apply bounded controlled-frame yaw with POSE_6DOF"
                        if orientation_policy
                        == "APPLY_CONTROLLED_FRAME_YAW_DELTA"
                        else "Position-only IK"
                    ),
                },
                {
                    "label": "Trigger",
                    "value": "Immediate approved MIT one-shot commit",
                },
                {
                    "label": "Preview",
                    "value": str(arguments.get("preview_id") or "unknown"),
                },
            ]
        elif tool_name == "review_and_activate_stationary_calibration":
            alignment_id = str(arguments.get("alignment_id") or "unknown")
            candidate_sha256 = str(
                arguments.get("candidate_sha256") or "unknown"
            )
            title = "Activate this exact world-to-arm calibration?"
            summary = (
                "The stationary calibration candidate will be recorded as "
                "reviewed and submitted to Manager for a bounded activation."
            )
            warning = (
                "No arm motion is submitted. If Manager accepts the current "
                "quality, provenance, VIO state, and age checks, this exact "
                "transform becomes motion-usable for at most five minutes."
            )
            confirm_label = "Approve exact calibration"
            details = [
                {"label": "Alignment", "value": alignment_id},
                {"label": "Candidate SHA-256", "value": candidate_sha256},
                {"label": "Activation", "value": "At most 5 minutes"},
                {"label": "Physical motion", "value": "None"},
            ]
        elif tool_name == "reinitialize_space_cognition":
            reason = str(arguments.get("reason") or "not provided")
            title = "Establish a new Midbrain spatial origin?"
            summary = (
                "The Agent is requesting a deliberate Local VIO epoch reset "
                "from the current stationary pose."
            )
            warning = (
                "This invalidates the current world coordinate epoch, revokes "
                "active stationary-workcell calibrations, and clears "
                "epoch-bound accumulated observations. The robot and camera "
                "must remain stationary."
            )
            confirm_label = "Approve new spatial origin"
            details = [
                {"label": "Reason", "value": reason},
                {
                    "label": "Calibration effect",
                    "value": "Active workcell calibration will be revoked",
                },
                {
                    "label": "Motion",
                    "value": "Inhibited during initialization",
                },
            ]
        elif tool_name == "execute_basic_safe_home":
            title = "Move the arm to configured safe-home?"
            summary = (
                "The Basic Controller will preempt active arm control, move "
                "the six arm joints to its configured home, preserve the "
                "measured gripper angle, and return to gravity float."
            )
            warning = (
                "This is physical arm motion. Keep clear of the arm and be "
                "ready to use the emergency stop. Approval starts safe-home "
                "immediately."
            )
            confirm_label = "Approve safe-home"
            details = [
                {"label": "Controller", "value": "robot_arm.rebot_dm"},
                {
                    "label": "Completion",
                    "value": "Controller-confirmed home and gravity float",
                },
            ]
        return {
            "tool_name": tool_name,
            "tool_namespace": item.tool_namespace,
            "title": title,
            "summary": summary,
            "warning": warning,
            "confirm_label": confirm_label,
            "details": details,
            "request": raw,
        }
