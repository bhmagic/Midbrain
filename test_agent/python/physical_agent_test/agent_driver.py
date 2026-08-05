from __future__ import annotations

import asyncio
import json
import logging
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

from .agent_events import translate_openai_sdk_events
from .basic_safe_home_adapter import BasicSafeHomeAdapter
from .effector_front_adapter import EffectorFrontSkillAdapter
from .gemini_pointing_skill import (
    PointingIdentificationSkill,
    VisualSceneAnalysisSkill,
)
from .integrated_motion_adapter import (
    JOINT_SPEED_AUTHENTICATION_THRESHOLD_RAD_S,
    MAX_CONTROLLED_FRAME_YAW_DELTA_DEG,
    DEFAULT_RELATIVE_NOMINAL_SPEED_M_S,
    MAX_RELATIVE_TRANSLATION_M,
    IntegratedRelativeMotionAdapter,
)
from .item_locator_adapter import MetricItemLocatorAdapter
from .manager_client import ManagerClient
from .no_contact_approach import NoContactItemApproachAdapter
from .phase4_policy import (
    await_with_progress_heartbeat,
    report_operation_progress,
)
from .rgbd_alignment import RgbdAlignmentValidationSkill
from .semantic_scene_inspector import SemanticSceneInspector
from .scene_segmentation_policy_publisher import (
    SceneSegmentationPolicyPublisher,
)
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
    auto_authorize_provider_stop: bool = False
    auto_authorize_relative_motion: bool = False
    max_auto_move_cm: float = 5.0
    max_auto_speed_m_s: float = DEFAULT_RELATIVE_NOMINAL_SPEED_M_S
    auto_authorize_stationary_calibration: bool = False
    auto_authorize_stationary_activation: bool = False
    auto_authorize_safe_home: bool = False
    auto_authorize_space_reinitialization: bool = False


AgentEventSink = Callable[[str, dict[str, Any]], Awaitable[None]]
AgentInput = str | list[dict[str, Any]] | RunState[Any]
logger = logging.getLogger(__name__)


def _current_user_text(input_value: AgentInput) -> str:
    if isinstance(input_value, str):
        return input_value.strip()
    if isinstance(input_value, RunState):
        return ""
    for item in reversed(input_value):
        if str(item.get("role") or "").lower() != "user":
            continue
        content = item.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                text = part.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
            return "\n".join(parts)
    return ""


def deterministic_intent_tool_route(prompt: str) -> dict[str, Any] | None:
    """Narrow tools for explicit spatial intents that must not degrade."""

    normalized = " ".join(str(prompt or "").lower().split())
    world_terms = any(
        term in normalized
        for term in ("world axis", "world axes", "world frame", "world origin")
    )
    excludes_arm = any(
        term in normalized
        for term in (
            "not the arm",
            "not arm",
            "without the arm",
            "exclude the arm",
            "world only",
        )
    )
    if world_terms and excludes_arm:
        return {
            "route": "WORLD_AXIS_ONLY",
            "allowed_tools": {
                "inspect_midbrain_runtime",
                "establish_world_axis",
                "tool_search",
            },
            "instruction": (
                "The current request explicitly excludes arm-base location. "
                "Never call calibrate_stationary_workcell or any arm-base pose "
                "locator. Call establish_world_axis so camera/VIO activation, "
                "the stationary initialization gate, and TRACKING body-pose "
                "verification remain one bounded operation. Report the returned "
                "world frame, session epoch, tracking state, and convention. "
                "This operation must not reset the origin."
            ),
        }

    scene_policy_terms = any(
        term in normalized
        for term in (
            "obstacle",
            "keep out",
            "keep-out",
            "pushable",
            "do not collide",
            "don't collide",
        )
    )
    if scene_policy_terms:
        return {
            "route": "EXPLICIT_SCENE_SEGMENTATION_POLICY",
            "allowed_tools": {
                "configure_scene_segmentation_policy",
                "inspect_arm_semantic_scene",
                "inspect_midbrain_runtime",
                "set_provider_residency",
                "plan_no_contact_item_approach",
                "execute_no_contact_approach_step",
                "tool_search",
            },
            "instruction": (
                "The user is explicitly defining scene semantics. Call "
                "configure_scene_segmentation_policy with only the objects "
                "the user described and their requested types. Never infer "
                "additional KEEP_OUT objects. Include a complete robot-arm "
                "description for the independent SAM2 arm exclusion mask. "
                "Unclaimed visible geometry remains PUSHABLE and non-blocking. "
                "Ensure perception.sam2_scene_tracker is HOT after publishing, "
                "then ensure world_model.arm_scene_compiler is HOT and call "
                "inspect_arm_semantic_scene. Do not report that the scan worked "
                "until inspection returns SCENE_READY with at least one "
                "KEEP_OUT sphere. The 3D viewer intentionally displays a "
                "reduced deterministic sample while the controller retains the "
                "complete scene. "
                "Only continue into approach planning when the same prompt "
                "also explicitly requests arm movement."
            ),
        }

    metric_terms = any(
        term in normalized
        for term in (
            "3d",
            "3-d",
            "metric",
            "depth",
            "world coordinate",
            "camera coordinate",
            "robot coordinate",
        )
    )
    item_terms = any(
        term in normalized
        for term in (
            "object",
            "item",
            "thing",
            "roll",
            "toilet paper",
            "workpiece",
        )
    )
    location_terms = any(
        term in normalized
        for term in ("locate", "location", "position", "where", "identify")
    )
    if metric_terms and item_terms and location_terms:
        return {
            "route": "METRIC_ITEM_LOCATION",
            "allowed_tools": {
                "inspect_midbrain_runtime",
                "set_provider_residency",
                "locate_item",
                "tool_search",
            },
            "instruction": (
                "This is an explicit metric item-location request. Use "
                "locate_item; analyze_visual_scene is RGB-only and cannot "
                "satisfy the request. Use target_frame=CURRENT_WORLD when the "
                "user wants world 3D without locating the arm base. If a "
                "required provider is cold, activate it and then invoke "
                "locate_item in the same run. Report degraded bearing-only "
                "evidence as degraded, not as a metric location."
            ),
        }
    approach_terms = any(
        term in normalized
        for term in (
            "close to",
            "move close",
            "move near",
            "move to",
            "approach",
            "get near",
            "above",
            "over the",
            "toward",
            "towards",
        )
    )
    effector_terms = any(
        term in normalized
        for term in ("gripper", "effector", "arm", "hand")
    )
    implicit_effector_motion = "move" in normalized and item_terms
    if (
        approach_terms
        and item_terms
        and (effector_terms or implicit_effector_motion)
    ):
        return {
            "route": "NO_CONTACT_ITEM_APPROACH",
            "allowed_tools": {
                "inspect_midbrain_runtime",
                "set_provider_residency",
                "plan_no_contact_item_approach",
                "execute_no_contact_approach_step",
                "calibrate_stationary_workcell",
                "review_and_activate_stationary_calibration",
                "tool_search",
            },
            "instruction": (
                "This request asks to move the effector close to an item. Use "
                "plan_no_contact_item_approach before any motion; do not ask "
                "the user to translate the visual target into a relative XYZ "
                "command and do not substitute preview_relative_effector_motion. "
                "If it returns ARM_BASE_REGISTRATION_REQUIRED, call its exact "
                "required_next_tool, complete candidate review/activation, then "
                "retry the preserved plan_no_contact_item_approach arguments. "
                "If it returns SEMANTIC_SCENE_PROVIDER_REQUIRED, activate its "
                "exact required provider and retry the preserved arguments in "
                "the same run. If the SAM2 tracker remains DEGRADED because no "
                "explicit KEEP_OUT policy exists, do not invent one from this "
                "move request and do not publish a work-object-only policy; "
                "report that explicit user obstacle descriptions are required. "
                "If it returns ITEM_OBSERVATION_REJECTED or "
                "EFFECTOR_OBSERVATION_REJECTED, reacquire both once with the "
                "same arguments; stop and report the evidence if the second "
                "observation is also rejected. "
                "If the controller preview is ready, invoke its exact "
                "required_next_tool arguments without editing them. After each "
                "executed step, treat WAITING_NEXT, HOLDING_FINAL, and verified "
                "COMPLETED_FLOAT as successful measured arrivals when the tool "
                "returns measured_arrival_confirmed=true. Immediately invoke "
                "the returned plan tool so both landmarks are reacquired and "
                "realigned; do not report WAITING_NEXT as unconfirmed motion. "
                "Stop only when aligned, a safety "
                "gate rejects the step, or the iteration limit is reached. A "
                "planning result alone must not be reported as completed movement. "
                "Use vertical_policy=PRESERVE_CURRENT_HEIGHT when the user "
                "explicitly asks for horizontal or same-height motion, "
                "NO_DESCENT when descent is explicitly forbidden, and FREE_3D "
                "otherwise."
            ),
        }
    return None


def _provider_readiness_snapshot(
    provider: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(provider, dict):
        return None
    report = provider.get("report")
    report = report if isinstance(report, dict) else {}
    return {
        "provider_id": str(
            provider.get("config", {}).get("id") or report.get("provider_id") or ""
        ),
        "process_state": provider.get("process_state"),
        "residency": report.get("residency"),
        "health": report.get("health"),
        "ready": bool(report.get("ready")),
        "expired": bool(report.get("expired")),
        "instance_id": report.get("instance_id"),
        "boot_id": report.get("boot_id"),
        "last_seen": report.get("last_seen"),
    }


async def wait_for_provider_hot_readiness(
    manager: ManagerClient,
    provider_id: str,
    *,
    required_capability: str | None,
    timeout_s: float,
    poll_interval_s: float = 0.25,
) -> dict[str, Any]:
    timeout = float(timeout_s)
    poll_interval = float(poll_interval_s)
    if not math.isfinite(timeout) or timeout <= 0.0:
        raise ValueError("provider HOT readiness timeout must be positive")
    if not math.isfinite(poll_interval) or poll_interval <= 0.0:
        raise ValueError("provider HOT readiness poll interval must be positive")

    deadline = asyncio.get_running_loop().time() + timeout
    latest_provider: dict[str, Any] | None = None
    latest_capability: dict[str, Any] | None = None
    while True:
        if required_capability is None:
            providers = await manager.providers()
            capabilities: list[dict[str, Any]] = []
        else:
            providers, capabilities = await asyncio.gather(
                manager.providers(),
                manager.capabilities(),
            )
        latest_provider = next(
            (
                provider
                for provider in providers
                if str(provider.get("config", {}).get("id") or "")
                == provider_id
            ),
            None,
        )
        snapshot = _provider_readiness_snapshot(latest_provider)
        provider_ready = bool(
            snapshot
            and snapshot["residency"] == "HOT"
            and snapshot["ready"]
            and not snapshot["expired"]
        )

        advertised_capabilities = [
            capability
            for capability in capabilities
            if capability.get("provider_id") == provider_id
        ]
        latest_capability = next(
            (
                capability
                for capability in advertised_capabilities
                if capability.get("capability") == required_capability
            ),
            None,
        )
        capability_advertised = (
            None if required_capability is None else latest_capability is not None
        )
        capability_ready = (
            None
            if required_capability is None or latest_capability is None
            else bool(
                latest_capability.get("available")
                and latest_capability.get("ready")
                and not latest_capability.get("expired")
            )
        )
        if provider_ready and capability_ready is not False:
            return {
                "status": "READY",
                "provider_ready": True,
                "required_capability": required_capability,
                "capability_advertised": capability_advertised,
                "capability_ready": capability_ready,
                "provider": snapshot,
                "capability": latest_capability,
                "advertised_capabilities": sorted(
                    str(capability.get("capability") or "")
                    for capability in advertised_capabilities
                    if capability.get("capability")
                ),
                "timeout_s": timeout,
            }

        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0.0:
            return {
                "status": "TIMED_OUT",
                "provider_ready": provider_ready,
                "required_capability": required_capability,
                "capability_advertised": capability_advertised,
                "capability_ready": capability_ready,
                "provider": snapshot,
                "capability": latest_capability,
                "advertised_capabilities": sorted(
                    str(capability.get("capability") or "")
                    for capability in advertised_capabilities
                    if capability.get("capability")
                ),
                "timeout_s": timeout,
            }
        report_operation_progress("WAIT_PROVIDER_HOT_READINESS")
        await asyncio.sleep(min(poll_interval, remaining))


def _session_authorization(context_wrapper: Any) -> AgentSessionAuthorization:
    authorization = getattr(context_wrapper, "context", None)
    if isinstance(authorization, AgentSessionAuthorization):
        return authorization
    return AgentSessionAuthorization()


def _runner_context(
    input_value: AgentInput,
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
    if action in {"start", "hot", "warm"}:
        return not authorization.auto_authorize_provider_activation
    if action == "stop":
        return not authorization.auto_authorize_provider_stop
    return True


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
        distance_m = float(arguments.get("distance_m"))
        planned_speed_m_s = float(
            arguments.get("planned_nominal_speed_m_s")
        )
        requested_peak_joint_speed = float(
            arguments.get("requested_peak_joint_speed_rad_s", 0.0)
        )
    except (TypeError, ValueError):
        return False
    if (
        not math.isfinite(maximum_cm)
        or maximum_cm <= 0.0
        or not math.isfinite(distance_m)
        or distance_m < 0.0
        or distance_m * 100.0 > maximum_cm + 1e-9
        or not math.isfinite(planned_speed_m_s)
        or planned_speed_m_s < 0.0
        or not math.isfinite(requested_peak_joint_speed)
        or requested_peak_joint_speed < 0.0
        or requested_peak_joint_speed >= 20.0
        or bool(arguments.get("joint_speed_authentication_required"))
        or requested_peak_joint_speed
        > JOINT_SPEED_AUTHENTICATION_THRESHOLD_RAD_S
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
                "PRESERVE_CURRENT",
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


async def safe_home_needs_approval(
    context_wrapper: Any,
    _arguments: dict[str, Any],
    _call_id: str,
) -> bool:
    authorization = _session_authorization(context_wrapper)
    return not authorization.auto_authorize_safe_home


async def space_reinitialization_needs_approval(
    context_wrapper: Any,
    _arguments: dict[str, Any],
    _call_id: str,
) -> bool:
    authorization = _session_authorization(context_wrapper)
    return not authorization.auto_authorize_space_reinitialization


async def consume_openai_agent_stream(
    result: Any,
    event_sink: AgentEventSink,
) -> Any:
    """Fully consume one SDK stream while isolating observer failures."""

    async for sdk_event in result.stream_events():
        for event_type, payload in translate_openai_sdk_events(sdk_event):
            try:
                await event_sink(event_type, payload)
            except Exception:
                logger.exception(
                    "Midbrain event observer failed for %s",
                    event_type,
                )
    return result


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
        item_locator_skill: MetricItemLocatorAdapter | None = None,
        effector_front_skill: EffectorFrontSkillAdapter | None = None,
        no_contact_approach_skill: (
            NoContactItemApproachAdapter | None
        ) = None,
        semantic_scene_inspector: SemanticSceneInspector | None = None,
        scene_policy_publisher: (
            SceneSegmentationPolicyPublisher | None
        ) = None,
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
        space_cognition_establisher: (
            Callable[[], Awaitable[dict[str, Any]]] | None
        ) = None,
        space_cognition_reinitializer: (
            Callable[[str], Awaitable[dict[str, Any]]] | None
        ) = None,
        session: Any | None = None,
        defer_loading: bool = False,
        adapter_timeout_s: float = 60.0,
        stationary_calibration_timeout_s: float = 600.0,
        provider_hot_readiness_timeout_s: float = 45.0,
        provider_hot_readiness_poll_interval_s: float = 0.25,
        max_turns: int = 16,
        session_history_item_limit: int | None = None,
    ):
        self.skill = skill
        self.max_turns = int(max_turns)
        if not 1 <= self.max_turns <= 32:
            raise ValueError("max_turns must be between 1 and 32")
        self.provider_hot_readiness_timeout_s = float(
            provider_hot_readiness_timeout_s
        )
        self.provider_hot_readiness_poll_interval_s = float(
            provider_hot_readiness_poll_interval_s
        )
        if (
            not math.isfinite(self.provider_hot_readiness_timeout_s)
            or self.provider_hot_readiness_timeout_s <= 0.0
        ):
            raise ValueError(
                "provider_hot_readiness_timeout_s must be positive"
            )
        if (
            not math.isfinite(self.provider_hot_readiness_poll_interval_s)
            or self.provider_hot_readiness_poll_interval_s <= 0.0
        ):
            raise ValueError(
                "provider_hot_readiness_poll_interval_s must be positive"
            )
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
        if space_cognition_establisher is not None:
            async def establish_world_axis_adapter(
                arguments: dict[str, Any],
            ) -> str:
                if arguments:
                    raise ValueError(
                        "establish_world_axis does not accept arguments"
                    )
                result = await space_cognition_establisher()
                return json.dumps(result, ensure_ascii=False, default=str)

            adapters[
                "skill.initialize_space_cognition.ensure_tracking.v1"
            ] = BoundMethodSkillAdapter(establish_world_axis_adapter)
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
        if item_locator_skill is not None:
            async def item_locator_adapter(
                arguments: dict[str, Any],
            ) -> str:
                result = await item_locator_skill.run(
                    question=arguments.get("question"),
                    target_frame=arguments.get("target_frame"),
                    object_id=arguments.get("object_id"),
                    contact_policy=arguments.get(
                        "contact_policy",
                        "WORKPIECE_CONTACT_ALLOWED",
                    ),
                    depth_requirement=arguments.get(
                        "depth_requirement",
                        "PREFER_METRIC",
                    ),
                    task_plane=arguments.get("task_plane"),
                )
                return json.dumps(result, ensure_ascii=False, default=str)

            adapters[
                "skill.observe_pointed_object.locate.v2"
            ] = BoundMethodSkillAdapter(item_locator_adapter)
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
        if no_contact_approach_skill is not None:
            async def no_contact_approach_adapter(
                arguments: dict[str, Any],
            ) -> str:
                result = await no_contact_approach_skill.run(
                    question=arguments.get("question"),
                    object_id=arguments.get("object_id"),
                    requested_standoff_m=arguments.get(
                        "requested_standoff_m",
                        0.10,
                    ),
                    iteration_index=arguments.get("iteration_index", 0),
                    maximum_iterations=arguments.get(
                        "maximum_iterations",
                        6,
                    ),
                    maximum_step_m=arguments.get("maximum_step_m", 1.2),
                    vertical_policy=arguments.get(
                        "vertical_policy", "FREE_3D"
                    ),
                )
                return json.dumps(result, ensure_ascii=False, default=str)

            adapters[
                "skill.approach_item_no_contact.plan.v1"
            ] = BoundMethodSkillAdapter(no_contact_approach_adapter)
        if semantic_scene_inspector is not None:
            async def semantic_scene_inspector_adapter(
                arguments: dict[str, Any],
            ) -> str:
                result = await semantic_scene_inspector.run(
                    include_spheres=arguments.get("include_spheres", False),
                    maximum_spheres=arguments.get("maximum_spheres", 100),
                )
                return json.dumps(result, ensure_ascii=False, default=str)

            adapters[
                "test_agent.inspect_arm_semantic_scene.v1"
            ] = BoundMethodSkillAdapter(semantic_scene_inspector_adapter)
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

        approval_overrides: dict[
            str,
            bool | Callable[..., Awaitable[bool]],
        ] = {}
        if stationary_calibration_skill is not None:
            approval_overrides["calibrate_stationary_workcell"] = (
                stationary_calibration_needs_approval
            )
        if (
            space_cognition_reinitializer is not None
            and "reinitialize_space_cognition" in eligible
        ):
            approval_overrides["reinitialize_space_cognition"] = (
                space_reinitialization_needs_approval
            )

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
            approval_overrides=approval_overrides or None,
        )
        self.offered_skill_descriptors = [
            descriptor
            for descriptor in descriptors
            if descriptor.tool_name in eligible
        ]

        offered_tools = list(tools)
        if defer_loading:
            offered_tools.append(ToolSearchTool())
        if scene_policy_publisher is not None:
            async def configure_scene_segmentation_policy(
                _context,
                raw_arguments: str,
            ) -> str:
                arguments = json.loads(raw_arguments)
                result = await scene_policy_publisher.publish_policy(
                    policy_id=arguments.get("policy_id"),
                    objects=arguments.get("objects"),
                    arm_description=arguments.get("arm_description"),
                )
                return json.dumps(result, ensure_ascii=False, default=str)

            offered_tools.append(
                FunctionTool(
                    name="configure_scene_segmentation_policy",
                    description=(
                        "Publish the user's explicit obstacle/work-object/"
                        "pushable descriptions to the Fabric for the HOT SAM2 "
                        "scene tracker. Only listed KEEP_OUT objects become "
                        "blocking geometry; unclaimed visible depth defaults "
                        "to ignored PUSHABLE geometry. This submits no motion."
                    ),
                    params_json_schema={
                        "type": "object",
                        "properties": {
                            "policy_id": {
                                "type": "string",
                                "minLength": 1,
                            },
                            "objects": {
                                "type": "array",
                                "minItems": 1,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "object_id": {
                                            "type": "string",
                                            "minLength": 1,
                                        },
                                        "type": {
                                            "type": "string",
                                            "enum": [
                                                "KEEP_OUT",
                                                "PUSHABLE",
                                                "WORK_OBJECT",
                                            ],
                                        },
                                        "description": {
                                            "type": "string",
                                            "minLength": 1,
                                        },
                                    },
                                    "required": [
                                        "object_id",
                                        "type",
                                        "description",
                                    ],
                                    "additionalProperties": False,
                                },
                            },
                            "arm_description": {
                                "type": "string",
                                "minLength": 1,
                            },
                        },
                        "required": [
                            "policy_id",
                            "objects",
                            "arm_description",
                        ],
                        "additionalProperties": False,
                    },
                    on_invoke_tool=configure_scene_segmentation_policy,
                    strict_json_schema=True,
                    needs_approval=False,
                )
            )
        if no_contact_approach_skill is not None:
            async def execute_no_contact_approach_step(
                _context,
                raw_arguments: str,
            ) -> str:
                arguments = json.loads(raw_arguments)
                result = await no_contact_approach_skill.execute_preview(
                    plan_id=arguments.get("plan_id"),
                )
                return json.dumps(result, ensure_ascii=False, default=str)

            async def no_contact_motion_needs_approval(
                context_wrapper: Any,
                arguments: dict[str, Any],
                _call_id: str,
            ) -> bool:
                authorization = _session_authorization(context_wrapper)
                canonical = await (
                    no_contact_approach_skill
                    .pending_execution_authorization_arguments(
                        arguments.get("plan_id")
                    )
                )
                return not (
                    canonical is not None
                    and authorization.auto_authorize_relative_motion
                    and relative_motion_within_authorization(
                        canonical,
                        max_auto_move_cm=authorization.max_auto_move_cm,
                        max_auto_speed_m_s=authorization.max_auto_speed_m_s,
                    )
                )

            offered_tools.append(
                FunctionTool(
                    name="execute_no_contact_approach_step",
                    description=(
                        "Execute one exact, fresh, collision-checked no-contact "
                        "item-approach preview by its opaque plan ID. All motion "
                        "parameters and controller digests are recovered from "
                        "the pending preview rather than copied by the model. "
                        "Host authorization immediately requests one bounded "
                        "physical correction of at most the planned step, after "
                        "which both item and effector must be observed again. "
                        "WAITING_NEXT and HOLDING_FINAL are measured-arrival "
                        "terminal states, not incomplete motion, when the result "
                        "sets measured_arrival_confirmed=true."
                    ),
                    params_json_schema={
                        "type": "object",
                        "properties": {
                            "plan_id": {"type": "string", "minLength": 1},
                        },
                        "required": ["plan_id"],
                        "additionalProperties": False,
                    },
                    on_invoke_tool=execute_no_contact_approach_step,
                    strict_json_schema=True,
                    needs_approval=no_contact_motion_needs_approval,
                )
            )
        if stationary_calibration_skill is not None:
            async def review_and_activate_stationary_calibration(
                _context,
                raw_arguments: str,
            ) -> str:
                try:
                    arguments = json.loads(raw_arguments)
                except json.JSONDecodeError:
                    continuation = (
                        stationary_calibration_skill
                        .latest_activation_continuation()
                    )
                    if continuation is None:
                        raise
                    arguments = dict(continuation["arguments"])
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
                        "publishes a persistent mounted-workcell transform. "
                        "Its motion usability is gated by current camera, "
                        "calibration, and VIO tracking identities rather than "
                        "a wall-clock expiry."
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
                required_capability = arguments.get("required_capability")
                if not isinstance(provider_id, str) or not provider_id.strip():
                    raise ValueError("provider_id must be non-empty text")
                if action not in {"start", "hot", "warm", "stop"}:
                    raise ValueError(
                        "action must be start, hot, warm, or stop"
                    )
                if required_capability is not None and (
                    not isinstance(required_capability, str)
                    or not required_capability.strip()
                ):
                    raise ValueError(
                        "required_capability must be non-empty text when supplied"
                    )
                if isinstance(required_capability, str):
                    required_capability = required_capability.strip()
                configured = {
                    str(provider.get("config", {}).get("id"))
                    for provider in await manager.providers()
                }
                if provider_id not in configured:
                    raise ValueError(
                        f"{provider_id} is not a configured Provider"
                    )
                result = await manager.set_residency(provider_id, action)
                readiness = None
                if action == "hot" or (
                    action == "start" and required_capability is not None
                ):
                    readiness = await wait_for_provider_hot_readiness(
                        manager,
                        provider_id,
                        required_capability=required_capability,
                        timeout_s=self.provider_hot_readiness_timeout_s,
                        poll_interval_s=(
                            self.provider_hot_readiness_poll_interval_s
                        ),
                    )
                lifecycle_complete = bool(
                    readiness is None or readiness["status"] == "READY"
                )
                if (
                    readiness is not None
                    and lifecycle_complete
                    and readiness.get("capability_advertised") is False
                ):
                    agent_instruction = (
                        "The Provider is HOT and ready, but the requested "
                        "capability name is not advertised by that Provider. "
                        "It was treated as an advisory model guess rather than "
                        "an impossible readiness gate. Do not invent a "
                        "replacement or request this identical lifecycle "
                        "transition again. Invoke the original finite Skill "
                        "immediately; its adapter validates the exact "
                        "capability needed for the operation."
                    )
                elif readiness is not None and lifecycle_complete:
                    agent_instruction = (
                        "The Provider is now HOT and ready. Do not request "
                        "this identical lifecycle transition again in the "
                        "current run. If a finite Skill required this "
                        "transition, invoke that original Skill immediately; "
                        "do not inspect the runtime again or finish before "
                        "the Skill returns. If the user requested only this "
                        "lifecycle change, report the observed readiness."
                    )
                elif readiness is not None:
                    agent_instruction = (
                        f"Manager accepted the {action.upper()} request, but "
                        "the Provider did not become HOT and ready before "
                        "the bounded timeout. Do "
                        "not request the identical transition again or claim "
                        "activation in this run. If the requested action was "
                        "START and a finite Skill still needs the capability, "
                        "call the reported required_next_tool for HOT. "
                        "Otherwise report the readiness timeout and its "
                        "latest evidence."
                    )
                else:
                    agent_instruction = (
                        "Do not request this identical lifecycle transition "
                        "again in the current run. Continue the original "
                        "finite task when this transition was its dependency."
                    )
                return json.dumps(
                    {
                        "lifecycle_request_accepted": True,
                        "lifecycle_request_complete": lifecycle_complete,
                        "provider_id": provider_id,
                        "requested_action": action.upper(),
                        "required_capability": required_capability,
                        "manager_result": result,
                        "readiness": readiness,
                        "required_next_tool": (
                            {
                                "name": "set_provider_residency",
                                "arguments": {
                                    "provider_id": provider_id,
                                    "action": "hot",
                                    "required_capability": required_capability,
                                },
                            }
                            if action == "start"
                            and required_capability is not None
                            and not lifecycle_complete
                            else None
                        ),
                        "agent_instruction": agent_instruction,
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
                                    "description": (
                                        "Use hot for a finite-Skill dependency, "
                                        "even when the Provider process is "
                                        "stopped; Manager starts it as part of "
                                        "HOT. Start alone is process-only, but "
                                        "when required_capability is non-null "
                                        "it also waits for natural HOT "
                                        "readiness."
                                    ),
                                },
                                "required_capability": {
                                    "type": ["string", "null"],
                                 "description": (
                                     "Exact capability that caused a cold "
                                     "dependency transition, such as "
                                     "camera.rgb. Copy it verbatim from a "
                                     "finite Skill result or the runtime "
                                     "catalog; never infer, shorten, or "
                                     "synthesize it. Set it to null when no "
                                     "exact capability was reported."
                                 ),
                                },
                            },
                            "required": [
                                "provider_id",
                                "action",
                                "required_capability",
                            ],
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
                    requested_peak_joint_speed_rad_s=arguments.get(
                        "requested_peak_joint_speed_rad_s"
                    ),
                    effective_peak_joint_speed_rad_s=arguments.get(
                        "effective_peak_joint_speed_rad_s"
                    ),
                    joint_speed_authentication_required=arguments.get(
                        "joint_speed_authentication_required"
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
                                    "maximum": MAX_RELATIVE_TRANSLATION_M,
                                },
                                "original_request_distance_m": {
                                    "type": "number",
                                    "minimum": 0.0,
                                    "maximum": MAX_RELATIVE_TRANSLATION_M,
                                },
                                "requested_speed_m_s": {
                                    "anyOf": [
                                        {
                                            "type": "number",
                                            "exclusiveMinimum": 0.0,
                                        },
                                        {"type": "null"},
                                    ],
                                },
                                "requested_duration_s": {
                                    "type": "number",
                                    "minimum": 0.05,
                                    "maximum": 60.0,
                                },
                                "planned_duration_s": {
                                    "type": "number",
                                    "minimum": 0.05,
                                    "maximum": 60.0,
                                },
                                "planned_nominal_speed_m_s": {
                                    "type": "number",
                                    "minimum": 0.0,
                                },
                                "timing_safety_limited": {
                                    "type": "boolean",
                                },
                                "requested_peak_joint_speed_rad_s": {
                                    "type": "number",
                                    "minimum": 0.0,
                                    "exclusiveMaximum": 20.0,
                                },
                                "effective_peak_joint_speed_rad_s": {
                                    "type": "number",
                                    "minimum": 0.0,
                                    "exclusiveMaximum": 20.0,
                                },
                                "joint_speed_authentication_required": {
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
                                "requested_peak_joint_speed_rad_s",
                                "effective_peak_joint_speed_rad_s",
                                "joint_speed_authentication_required",
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
                        "float. Host session policy may authorize the exact "
                        "operation without an interactive prompt; controller "
                        "safety checks remain authoritative. It reports only "
                        "controller-confirmed completion."
                    ),
                    params_json_schema={
                        "type": "object",
                        "properties": {},
                        "required": [],
                        "additionalProperties": False,
                    },
                    on_invoke_tool=execute_basic_safe_home,
                    strict_json_schema=True,
                    needs_approval=safe_home_needs_approval,
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
                "robot_arm.rebot_dm to HOT first with exact capability "
                "robot.motion.arm.basic, then activate "
                "robot_arm.primary.integrated to HOT with exact capability "
                "robot.motion.arm.integrated.pos_vel.one_shot. Only after both "
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
                    "When a Skill reports a required_capability, copy it into "
                    "the lifecycle call and use action=hot even when the "
                    "Provider process is stopped; Manager starts a stopped "
                    "Provider as part of HOT. The tool waits for that exact "
                    "capability. If you used start with a required capability, "
                    "do not invoke the Skill unless that call reports ready; "
                    "follow required_next_tool when it times out. After an "
                    "approved activation reports ready, "
                    "invoke the original finite Skill immediately within the "
                    "same run; do not inspect the runtime again or stop at a "
                    "lifecycle summary."
                )
            if integrated_motion_skill is not None:
                instructions += (
                    " For a requested relative end-effector motion, inspect "
                    "the current runtime even if earlier conversation says the "
                    "Providers were running. Activate robot_arm.rebot_dm to "
                    "HOT first with exact capability robot.motion.arm.basic, "
                    "then activate robot_arm.primary.integrated to HOT with "
                    "exact capability "
                    "robot.motion.arm.integrated.pos_vel.one_shot. Only after "
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
                    " FoundationPose is retained as a slow explicit "
                    "initializer, not as the default world-to-arm alignment "
                    "route. Call calibrate_stationary_workcell only when the "
                    "operator's complete request is exactly: 'Use "
                    "FoundationPose to establish the stationary world-to-arm-"
                    "base transform.' Pass that sentence unchanged as the "
                    "request argument. For every other establish, calibrate, "
                    "or validate request, do not call FoundationPose; use the "
                    "movement-based gripper alignment workflow when it is "
                    "available and otherwise report that it is not yet "
                    "implemented. Do not ask for "
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
                    "motion_usable=true. Candidate review and mounted-rig "
                    "activation have their own authorization policy. The "
                    "activation has no wall-clock expiry; Manager gates it "
                    "using exact camera/VIO identity, calibration revision, "
                    "VIO epoch and convention, and current tracking health. "
                    "If an explicitly requested FoundationPose activation "
                    "returns FRESH_CALIBRATION_REQUIRED, never "
                    "retry that alignment. When the current user request is "
                    "the same exact FoundationPose sentence, call "
                    "calibrate_stationary_workcell again with the current "
                    "request and activate only its new candidate. "
                    "The Skill acquires "
                    "global motion inhibit, so "
                    "a later Integrated motion requires a fresh explicit "
                    "approved HOT transition before preview."
                )
            instructions += (
                " A request for only the world axis, world frame, or world "
                "origin that says not the arm base is not a stationary "
                "workcell-calibration request. Use establish_world_axis and "
                "never run calibrate_stationary_workcell for that wording. "
                "establish_world_axis may transiently inhibit robot motion to "
                "collect stationary IMU samples, but it never resets the VIO "
                "epoch or locates the arm base. A request to "
                "identify and locate an item in 3D, metric, depth, world, "
                "camera, or robot coordinates must use locate_item, never the "
                "RGB-only analyze_visual_scene tool. Use CURRENT_WORLD when "
                "the caller wants the live VIO world frame without an arm-base "
                "relationship."
            )
        if basic_safe_home_skill is not None:
            instructions += (
                " For an explicit safe-home request, use "
                "execute_basic_safe_home after ensuring the Basic Provider "
                "is running. Safe-home preempts active arm control and uses "
                "the active host authorization policy. Do not substitute "
                "gravity float, Provider "
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
                "from spatial drift. It is not a readiness probe. Host "
                "authorization "
                "revokes active workcell calibrations, resets the VIO epoch, "
                "and clears observations bound to the old epoch. After it "
                "succeeds, any world-to-arm calibration required by a later "
                "motion task must be established again."
            )
        instructions += (
            " A user-supplied image in the current message is contextual "
            "input only. It is not a live robot-camera observation and has "
            "no Provider identity, capture freshness, depth, calibration, "
            "spatial-frame, or physical-action authority. Never use it to "
            "satisfy a finite Skill's live-sensor requirement."
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
        input_value: AgentInput,
        *,
        model_override: str | None = None,
        reasoning_effort: str | None = None,
        vlm_model_override: str | None = None,
        authorization: AgentSessionAuthorization | None = None,
        event_sink: AgentEventSink | None = None,
    ) -> InteractiveAgentResult:
        result = await self._run(
            input_value,
            model_override=model_override,
            reasoning_effort=reasoning_effort,
            vlm_model_override=vlm_model_override,
            authorization=authorization,
            event_sink=event_sink,
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
        input_value: AgentInput,
        *,
        model_override: str | None = None,
        reasoning_effort: str | None = None,
        vlm_model_override: str | None = None,
        authorization: AgentSessionAuthorization | None = None,
        event_sink: AgentEventSink | None = None,
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
                        reasoning={
                            "effort": reasoning_effort,
                            "summary": "auto",
                        }
                    )
                ),
            )
        vlm_token = set_vlm_model_selection(vlm_model_override)
        try:
            runner_arguments = {
                "context": _runner_context(input_value, authorization),
                "max_turns": self.max_turns,
                "run_config": run_config,
                "session": self.session,
            }
            selected_agent = self.agent
            intent_route = deterministic_intent_tool_route(
                _current_user_text(input_value)
            )
            if intent_route is not None and not isinstance(input_value, RunState):
                allowed_tools = intent_route["allowed_tools"]
                selected_agent = self.agent.clone(
                    tools=[
                        tool
                        for tool in self.agent.tools
                        if getattr(tool, "name", "") in allowed_tools
                    ],
                    instructions=(
                        f"{self.agent.instructions} "
                        f"Deterministic intent route "
                        f"{intent_route['route']}: "
                        f"{intent_route['instruction']}"
                    ),
                )
            if event_sink is None:
                awaitable = Runner.run(
                    selected_agent,
                    input_value,
                    **runner_arguments,
                )
            else:
                awaitable = consume_openai_agent_stream(
                    Runner.run_streamed(
                        selected_agent,
                        input_value,
                        **runner_arguments,
                    ),
                    event_sink,
                )
            result = await await_with_progress_heartbeat(
                awaitable,
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
                "reviewed and submitted to Manager as a mounted-rig "
                "identity- and tracking-gated activation."
            )
            warning = (
                "No arm motion is submitted. If Manager accepts the current "
                "quality, provenance, provider identity, VIO epoch, and "
                "tracking checks, this exact transform remains usable until "
                "revoked, superseded, or its evidence identity changes."
            )
            confirm_label = "Approve exact calibration"
            details = [
                {"label": "Alignment", "value": alignment_id},
                {"label": "Candidate SHA-256", "value": candidate_sha256},
                {
                    "label": "Activation",
                    "value": "No timer; identity and tracking gated",
                },
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
