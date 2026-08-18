from __future__ import annotations

import asyncio
import copy
import json
import logging
import math
import os
from collections.abc import Awaitable, Callable, Mapping
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
from jsonschema import validate

from .agent_events import translate_openai_sdk_events
from .basic_safe_home_adapter import BasicSafeHomeAdapter
from .effector_front_adapter import EffectorFrontSkillAdapter
from .fabric_spatial_translation import FabricSpatialTranslator
from .fabric_world_point import FabricWorldPointComposer
from .gemini_pointing_skill import (
    PointingIdentificationSkill,
    VisualSceneAnalysisSkill,
)
from .integrated_motion_adapter import (
    JOINT_SPEED_AUTHENTICATION_THRESHOLD_RAD_S,
    MAX_CONTROLLED_FRAME_YAW_DELTA_DEG,
    DEFAULT_RELATIVE_NOMINAL_SPEED_M_S,
    IntegratedRelativeMotionAdapter,
)
from .item_locator_adapter import MetricItemLocatorAdapter
from .manager_client import ManagerClient
from .no_contact_approach import NoContactItemApproachAdapter
from .phase4_policy import (
    await_with_progress_heartbeat,
    extend_current_operation_hard_timeout,
    report_operation_progress,
)
from .prepared_action import CallScopedPreparedActionCoordinator
from .rgbd_alignment import RgbdAlignmentValidationSkill
from .semantic_scene_inspector import SemanticSceneInspector
from .scene_segmentation_policy_publisher import (
    SceneSegmentationPolicyPublisher,
)
from .reviewed_observation_execution import (
    ReviewedObservationExecutionAdapter,
)
from .result_projection import (
    finalize_skill_result,
    redact_credential_values,
    select_json_pointer,
    select_result_detail,
)
from .skill_catalog import describe_output_schema, discover_agent_skills
from .skill_execution import (
    BoundMethodSkillAdapter,
    HostedModelRouteProfile,
    HostedSkillInvocationBroker,
    SkillExecutionAdapter,
    SkillInvocationBrokerHandle,
    build_agent_tools,
    reset_graph_authoring_repair_state,
    reset_hosted_child_event_sink,
    set_graph_authoring_repair_state,
    set_hosted_child_event_sink,
)
from .skill_result_details import SkillResultDetailStore
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
PERFORM_RELATIVE_EFFECTOR_MOTION_TOOL = "perform_relative_effector_motion"
MOVE_EFFECTOR_TO_WORLD_POINT_TOOL = "move_effector_to_world_point"
DERIVE_FABRIC_WORLD_POINT_TOOL = "derive_fabric_world_point"
TRANSLATE_FABRIC_DIRECTION_TOOL = "translate_fabric_direction_to_world"
TRANSLATE_FABRIC_POSE_TOOL = "translate_fabric_pose_to_world"
OFFSET_WORLD_POINT_TOOL = "offset_world_point"
CONFIGURE_SCENE_POLICY_AND_INSPECT_RUNTIME_TOOL = (
    "configure_scene_policy_and_inspect_runtime"
)


def _scene_policy_parameters_schema() -> dict[str, Any]:
    """Return the shared strict input schema for scene-policy publication."""

    return {
        "type": "object",
        "properties": {
            "policy_id": {"type": "string", "minLength": 1},
            "objects": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "object_id": {"type": "string", "minLength": 1},
                        "type": {
                            "type": "string",
                            "enum": ["KEEP_OUT", "PUSHABLE", "WORK_OBJECT"],
                        },
                        "description": {"type": "string", "minLength": 1},
                    },
                    "required": ["object_id", "type", "description"],
                    "additionalProperties": False,
                },
            },
            "arm_description": {"type": "string", "minLength": 1},
        },
        "required": ["policy_id", "objects", "arm_description"],
        "additionalProperties": False,
    }


LIMITED_GRAPH_AGENT_GUIDANCE = (
    " IMPORTANT: Strongly prefer run_limited_graph whenever two or more "
    "known finite Skills form one predetermined sequential workflow, "
    "structured branch, or bounded read-only refinement loop. Load and submit "
    "the complete graph "
    "before invoking any graph child directly; do not begin a direct "
    "multi-Skill sequence and switch to graph later. The graph must include "
    "every requested graph-eligible stage, including later motion or cutting "
    "stages; never submit only a prefix and leave known stages outside it. A "
    "successful terminal must be reachable only after all requested stages "
    "have completed. Use direct Skill calls "
    "only for a single operation, open-ended replanning, or a host operation "
    "that is not graph-eligible. After any necessary non-Skill host setup, "
    "strongly prefer Limited Graph for the remaining finite Skill sequence. "
    "A Limited Graph must declare every node and edge up front, must not "
    "contain another graph, and must provide a terminal path plus strict "
    "runtime, transition, visit, model-route, physical-action, and "
    "retained-result limits. Retry only READ_ONLY children. Use MODEL_ROUTE "
    "only through a named host profile and only to select a predeclared edge; "
    "always supply a deterministic fallback. Never place credentials, "
    "authorization assertions, or signed action tokens in graph values. The "
    "host re-evaluates every exact child invocation. Before writing any "
    "binding, read the declared structured-result pointers appended to each "
    "child tool description. Use those exact nested output paths and exact "
    "destination input paths; never guess, flatten, or rename a field. The "
    "host rejects undeclared source and target pointers before the first child "
    "runs. A non-success Limited Graph result terminates that submitted "
    "workflow. Never invoke a failed graph child or any remaining graph stage "
    "directly afterward, and never treat an explicit no-action-submitted "
    "failure as permission for an out-of-graph physical retry. Replan only as "
    "a new complete bounded graph when a materially different plan is "
    "warranted. A repeated user message is a fresh request unless the user "
    "explicitly asks to resume prior partial progress."
)
LIMITED_GRAPH_ROUTED_REMINDER = (
    " This routed tool surface includes run_limited_graph. Apply the graph-first "
    "guidance after the route-specific instructions: submit the complete graph "
    "covering every requested graph-eligible stage before directly calling two "
    "or more finite Skills. Do not submit a prefix-only graph, and do not treat "
    "a tool-search result as graph execution."
)


def _select_routed_tools(
    tools: list[Any],
    allowed_tools: set[str],
    *,
    include_limited_graph: bool = True,
) -> list[Any]:
    """Filter capability tools while preserving required SDK infrastructure."""

    selected = [
        tool
        for tool in tools
        if getattr(tool, "name", "") in allowed_tools
    ]
    selected_function_count = sum(
        isinstance(tool, FunctionTool) for tool in selected
    )
    if include_limited_graph and selected_function_count >= 2 and not any(
        getattr(tool, "name", "") == "run_limited_graph"
        for tool in selected
    ):
        graph_tools = [
            tool
            for tool in tools
            if getattr(tool, "name", "") == "run_limited_graph"
        ]
        if len(graph_tools) > 1:
            raise RuntimeError("Agent surface has duplicate Limited Graph tools")
        selected.extend(graph_tools)
    deferred_selected = any(
        (
            isinstance(tool, FunctionTool)
            and bool(tool.defer_loading)
        )
        or (
            isinstance(getattr(tool, "tool_config", None), Mapping)
            and bool(tool.tool_config.get("defer_loading"))
        )
        for tool in selected
    )
    if not deferred_selected or any(
        isinstance(tool, ToolSearchTool) for tool in selected
    ):
        return selected

    search_tools = [
        tool for tool in tools if isinstance(tool, ToolSearchTool)
    ]
    if len(search_tools) != 1:
        raise RuntimeError(
            "A routed deferred-loading tool surface requires exactly one "
            "ToolSearchTool"
        )
    selected.append(search_tools[0])
    return selected


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
    safe_home_requested = any(
        term in normalized
        for term in (
            "safe home",
            "safe-home",
            "home the arm",
            "return the arm home",
            "send the arm home",
        )
    )
    safe_home_companion_terms = any(
        term in normalized
        for term in (
            "slice",
            "slicing",
            "cut with the blade",
            "map out",
            "obstacle",
            "workpiece",
            "work piece",
            "corner",
            "world axis",
            "world frame",
            "foundationpose",
            "foundation pose",
            "calibrat",
            "align",
            "inspect",
            "locate",
            "move above",
            "move the hand",
            "move the gripper",
        )
    )
    if safe_home_requested and not safe_home_companion_terms:
        return {
            "route": "SAFE_HOME",
            "allow_limited_graph": False,
            "allowed_tools": {
                "execute_basic_safe_home",
                "inspect_midbrain_runtime",
                "set_provider_residency",
            },
            "instruction": (
                "The operator explicitly requested Safe Home. Call "
                "execute_basic_safe_home directly. It is a host operation, "
                "not a Limited Graph child Skill. Do not deny the request or "
                "claim that the tool is unavailable when it is present on "
                "this routed surface. Follow a typed dependency continuation "
                "only if the Safe Home result returns one, and report success "
                "only from controller-confirmed completion."
            ),
        }
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

    item_terms = any(
        term in normalized
        for term in (
            "object",
            "item",
            "thing",
            "roll",
            "toilet paper",
            "workpiece",
            "work piece",
            "working piece",
        )
    )
    bounds_terms = any(
        term in normalized
        for term in (
            "bounding box",
            "aabb",
            "corner",
            "right-forward",
            "right forward",
            "left-forward",
            "left forward",
            "right-backward",
            "right backward",
            "left-backward",
            "left backward",
        )
    )
    effector_terms = any(
        term in normalized
        for term in ("gripper", "effector", "arm", "hand")
    )
    explicit_motion_terms = any(
        term in normalized
        for term in (
            "move",
            "position",
            "place",
            "send",
            "bring",
        )
    )
    motion_negated = any(
        term in normalized
        for term in (
            "do not move",
            "don't move",
            "without moving",
            "no movement",
            "not move",
        )
    )
    work_object_motion_terms = (
        item_terms
        and bounds_terms
        and effector_terms
        and explicit_motion_terms
        and not motion_negated
    )
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
    slicing_terms = any(
        term in normalized
        for term in ("slice", "slicing", "cut with the blade", "blade cut")
    )
    mixed_frame_terms = any(
        term in normalized
        for term in (
            "arm base",
            "arm-base",
            "hand axis",
            "hand axes",
            "effector axis",
            "effector axes",
            "controlled effector",
        )
    )
    if (
        scene_policy_terms
        and work_object_motion_terms
        and slicing_terms
        and mixed_frame_terms
    ):
        return {
            "route": "SCENE_CORNER_MOTION_AND_MIXED_FRAME_SLICING",
            "allowed_tools": {
                CONFIGURE_SCENE_POLICY_AND_INSPECT_RUNTIME_TOOL,
                "set_provider_residency",
                "inspect_arm_semantic_scene",
                DERIVE_FABRIC_WORLD_POINT_TOOL,
                MOVE_EFFECTOR_TO_WORLD_POINT_TOOL,
                TRANSLATE_FABRIC_DIRECTION_TOOL,
                OFFSET_WORLD_POINT_TOOL,
                "slice_with_blade",
                "tool_search",
            }
            | ({"execute_basic_safe_home"} if safe_home_requested else set()),
            "instruction": (
                "This request combines scene semantics, an absolute work-object "
                "corner move, and one or more mixed-frame slicing stages. "
                "Call configure_scene_policy_and_inspect_runtime once with only "
                "the requested scene objects. Use its fresh compact runtime "
                "catalog to request the arm scene compiler HOT in the next "
                "tool call; do not call inspect_midbrain_runtime separately. "
                "Then submit one "
                "complete Limited Graph whose first Skill node is "
                "inspect_arm_semantic_scene and which contains every remaining requested "
                "graph-eligible stage in order; never submit a corner-move "
                "prefix that omits later cutting or repositioning stages. Use "
                "derive_fabric_world_point for the named corner and unchanged "
                "offset, bind its world target unchanged into each requested "
                "absolute return move, and use "
                "translate_fabric_direction_to_world for every arm-base or "
                "controlled-effector direction. Keep world directions "
                "unchanged. Bind translated direction_world unchanged into "
                "the matching slicing field. A slicing begin offset from the "
                "current IK uses point_mode="
                "RELATIVE_TO_CURRENT_EFFECTOR_WORLD. Use null profile selectors "
                "unless the operator explicitly requested profile numbers. If "
                "a later target is an offset from an earlier Skill result, use "
                "offset_world_point: bind the earlier point and its world-frame "
                "identity unchanged, and supply the operator's exact offset, "
                "unit, and reference. For an offset above the first slicing "
                "point, bind /plan/path/slice_begin_point_world_m and "
                "/plan/workcell_binding/world_frame from that slicing node; "
                "never substitute /plan/path/planned_retract_endpoint_world_m. "
                "Give every physical request its own predetermined SKILL node; "
                "never retry or loop a physical node. A successful terminal "
                "must follow all requested stages and explicit complete child "
                "results. If Safe Home was also requested, call "
                "execute_basic_safe_home directly only after the graph "
                "completes; it is intentionally not a graph child and is not "
                "a reason to deny the preceding workflow."
            ),
        }
    if work_object_motion_terms and slicing_terms and mixed_frame_terms:
        return {
            "route": "WORK_OBJECT_MOTION_AND_MIXED_FRAME_SLICING",
            "allowed_tools": {
                "inspect_midbrain_runtime",
                "set_provider_residency",
                "inspect_arm_semantic_scene",
                DERIVE_FABRIC_WORLD_POINT_TOOL,
                MOVE_EFFECTOR_TO_WORLD_POINT_TOOL,
                TRANSLATE_FABRIC_DIRECTION_TOOL,
                OFFSET_WORLD_POINT_TOOL,
                "slice_with_blade",
                "tool_search",
            }
            | ({"execute_basic_safe_home"} if safe_home_requested else set()),
            "instruction": (
                "This request combines an absolute work-object corner move "
                "with one or more mixed-frame slicing stages under the "
                "existing scene policy. Submit one complete Limited Graph "
                "starting with inspect_arm_semantic_scene and containing every "
                "requested graph-eligible stage in order; "
                "never submit only the corner move or only the slice. Use "
                "derive_fabric_world_point for the named corner and unchanged "
                "offset, then bind its world target fields unchanged into "
                "move_effector_to_world_point. Use "
                "translate_fabric_direction_to_world for every arm-base or "
                "controlled-effector direction and keep world directions "
                "unchanged. Bind direction_world unchanged into the matching "
                "slicing field. A slicing begin offset from current IK uses "
                "point_mode=RELATIVE_TO_CURRENT_EFFECTOR_WORLD. Use null "
                "profile selectors unless the operator explicitly requested "
                "profile numbers. Use offset_world_point for any later target "
                "defined relative to an earlier Skill result. For a target "
                "above the first slice point, bind that slicing node's "
                "/plan/path/slice_begin_point_world_m and "
                "/plan/workcell_binding/world_frame; never substitute its "
                "planned retract endpoint. Give every physical request its own "
                "predetermined SKILL node and never retry or loop it. If Safe "
                "Home was also requested, call execute_basic_safe_home "
                "directly after graph completion; it is not graph-eligible "
                "and must not be treated as unavailable."
            ),
        }
    if scene_policy_terms and work_object_motion_terms:
        return {
            "route": "SCENE_POLICY_AND_WORK_OBJECT_WORLD_POINT_MOTION",
            "allowed_tools": {
                CONFIGURE_SCENE_POLICY_AND_INSPECT_RUNTIME_TOOL,
                "set_provider_residency",
                DERIVE_FABRIC_WORLD_POINT_TOOL,
                MOVE_EFFECTOR_TO_WORLD_POINT_TOOL,
            },
            "instruction": (
                "This request first defines scene semantics and then requests "
                "a work-object corner motion. Call "
                "configure_scene_policy_and_inspect_runtime with only the objects "
                "the user described and their requested types. Never infer "
                "additional KEEP_OUT objects. Include a complete robot-arm "
                "description for the independent SAM2 arm exclusion mask. "
                "Use the returned fresh compact runtime catalog, without a "
                "separate inspect_midbrain_runtime call, and then request "
                "world_model.arm_scene_compiler HOT with exact "
                "required_capability=world_model.arm.semantic_scene; Manager "
                "owns transitive activation of its declared dependencies. "
                "Do not call inspect_arm_semantic_scene: call "
                "derive_fabric_world_point directly with the configured "
                "WORK_OBJECT object_id, canonical named corner, unchanged "
                "numeric offset and unit, matching offset_reference, and a "
                "null expected_scene_revision. The coordinate Skill waits "
                "for and binds one coherent current Fabric snapshot. Never "
                "perform coordinate addition, unit conversion, transform "
                "math, or current-pose subtraction in the language model. "
                "Only after WORLD_POINT_READY, call "
                "move_effector_to_world_point once, copying "
                "target_position_world_m, target_world_frame_id, and "
                "target_session_epoch unchanged. Do not use no-contact "
                "approach tools for this explicit absolute corner target or "
                "report completion unless physical_motion_completed=true."
            ),
        }
    if scene_policy_terms:
        return {
            "route": "EXPLICIT_SCENE_SEGMENTATION_POLICY",
            "allowed_tools": {
                CONFIGURE_SCENE_POLICY_AND_INSPECT_RUNTIME_TOOL,
                "inspect_arm_semantic_scene",
                "set_provider_residency",
                "plan_no_contact_item_approach",
                "execute_no_contact_approach_step",
                "tool_search",
            },
            "instruction": (
                "The user is explicitly defining scene semantics. Call "
                "configure_scene_policy_and_inspect_runtime with only the objects "
                "the user described and their requested types. Never infer "
                "additional KEEP_OUT objects. Include a complete robot-arm "
                "description for the independent SAM2 arm exclusion mask. "
                "Unclaimed visible geometry remains PUSHABLE and non-blocking. "
                "Use the returned fresh compact runtime catalog, without a "
                "separate inspect_midbrain_runtime call. After publishing, "
                "request only "
                "world_model.arm_scene_compiler HOT with exact required_capability="
                "world_model.arm.semantic_scene; Manager owns transitive "
                "activation of its declared SAM2, camera, and Basic "
                "dependencies. Then call inspect_arm_semantic_scene. Do not "
                "report that the scan worked "
                "until inspection returns SCENE_READY with at least one "
                "KEEP_OUT sphere. The 3D viewer intentionally displays a "
                "reduced deterministic sample while the controller retains the "
                "complete scene. "
                "Only continue into approach planning when the same prompt "
                "also explicitly requests arm movement."
            ),
        }

    if slicing_terms and mixed_frame_terms:
        return {
            "route": "MIXED_FRAME_SLICING",
            "allowed_tools": {
                "inspect_midbrain_runtime",
                "set_provider_residency",
                TRANSLATE_FABRIC_DIRECTION_TOOL,
                "slice_with_blade",
                "tool_search",
            },
            "instruction": (
                "This slicing request contains directions expressed in more "
                "than one coordinate frame. Keep directions already stated "
                "in world axes unchanged. For each direction stated in "
                "arm-base or controlled-effector axes, first call "
                "translate_fabric_direction_to_world with the exact numeric "
                "vector and matching source_reference. Use null source "
                "identity fields when the direction came from the operator's "
                "current request rather than timestamped upstream evidence. "
                "After WORLD_DIRECTION_READY, copy direction_world unchanged "
                "into the matching semantic slicing field: blade direction "
                "goes only to blade_direction_world and slicing direction "
                "goes only to slicing_direction_world. Never swap those roles "
                "or perform transform math in the language model. A begin "
                "offset from current IK/current effector in world axes uses "
                "point_mode=RELATIVE_TO_CURRENT_EFFECTOR_WORLD and the exact "
                "metre offset directly. Then call slice_with_blade once after "
                "all required world directions are available. Use null for "
                "both profile selectors unless the operator explicitly named "
                "profile numbers."
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
    if work_object_motion_terms:
        return {
            "route": "SEMANTIC_WORK_OBJECT_WORLD_POINT_MOTION",
            "allowed_tools": {
                "inspect_midbrain_runtime",
                "set_provider_residency",
                DERIVE_FABRIC_WORLD_POINT_TOOL,
                MOVE_EFFECTOR_TO_WORLD_POINT_TOOL,
            },
            "instruction": (
                "This is a compound semantic-coordinate and free-space "
                "motion request. Do not call inspect_arm_semantic_scene. "
                "Call derive_fabric_world_point directly with the exact "
                "WORK_OBJECT object_id established by the current scene "
                "policy or prior structured result, the canonical named corner, "
                "and the user's unchanged numeric offset and unit. Select "
                "a zero METRES offset when no offset was requested. Select "
                "SOURCE_FRAME for arm-base/AABB axes, ACTIVE_WORLD for world "
                "axes, or CONTROLLED_EFFECTOR_FRAME for hand axes. Never "
                "perform coordinate addition, unit conversion, transform "
                "math, or current-pose subtraction in the language model. "
                "Use null expected_scene_revision unless a structured "
                "upstream result supplied one. The coordinate Skill performs "
                "the single authoritative scene read and binds one coherent "
                "current Fabric snapshot at invocation. If no exact object_id "
                "is available, report that ambiguity instead of inventing one. "
                "Only after WORLD_POINT_READY, call "
                "move_effector_to_world_point once, copying "
                "target_position_world_m, target_world_frame_id, and "
                "target_session_epoch unchanged. Do not substitute a "
                "relative move or report completion unless the motion result "
                "sets physical_motion_completed=true. A visible-surface AABB "
                "is not a tracked solid extent or contact authorization."
            ),
        }
    if item_terms and bounds_terms:
        return {
            "route": "SEMANTIC_WORK_OBJECT_BOUNDS",
            "allowed_tools": {
                "inspect_midbrain_runtime",
                "set_provider_residency",
                "inspect_arm_semantic_scene",
                DERIVE_FABRIC_WORLD_POINT_TOOL,
                "tool_search",
            },
            "instruction": (
                "This request refers to a work-object bound or named corner. "
                "Use inspect_arm_semantic_scene and select the fresh "
                "VISIBLE_SURFACE_AABB whose object_id or description matches "
                "the requested object. Use only its named corners_m values "
                "and report frame_id, observed_at_us, expires_at_us, and that "
                "the extent covers the currently visible surface. In the "
                "canonical arm-base convention, forward is +X, right is -Y, "
                "and up is +Z. Do not substitute the first sphere or infer a "
                "tracked solid extent. If no matching fresh AABB exists, "
                "report that explicitly instead of guessing. When the user "
                "requests an additive offset but no motion, call "
                "derive_fabric_world_point so the Skill performs the unit, "
                "vector, and transform math. This route is read-only and "
                "never authorizes movement."
            ),
        }
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
                "inspect_arm_semantic_scene",
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
            "until reaching",
            "until it reaches",
            "until reach",
            "reach the",
            "reaching the",
        )
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
                "This request asks for boundary-seeking free-space motion near "
                "an item. Treat wording such as reach, touch, until reaching, "
                "or until touching as a no-contact boundary target unless the "
                "user explicitly requests sustained force work such as pushing, "
                "pressing, cutting, scraping, or gripping. Boundary wording is "
                "neither contact authorization nor a reason to refuse movement. "
                "Use "
                "plan_no_contact_item_approach before any motion; do not ask "
                "the user to translate the visual target into a relative XYZ "
                "command and do not substitute generic relative motion. "
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
                "returns measured_arrival_confirmed=true. Never execute a "
                "consumed preview again. Immediately invoke the exact name and "
                "arguments in the returned required_next_tool so both landmarks "
                "are reacquired and realigned; do not report WAITING_NEXT as "
                "unconfirmed motion. "
                "When execution returns COMPLETED_CLOSEST_SAFE, stop the "
                "free-space workflow and report its closest_safe_report as a "
                "successful no-contact boundary result; do not retry through "
                "the object. Integrated allows zero extra WORK_OBJECT clearance "
                "while still forbidding intersection, and it preserves 10 mm "
                "clearance from KEEP_OUT obstacles. Intentional force/contact "
                "work requires a different skill and controller. "
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
    details = report.get("details")
    details = details if isinstance(details, dict) else {}
    diagnostics = details.get("diagnostics")
    diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
    diagnostic_summary = {
        key: diagnostics[key]
        for key in (
            "status",
            "coverage",
            "mapping_failure",
            "annotation_error",
            "segmentation_errors",
            "quality_review",
            "vlm_router",
            "transform_error",
            "blocking_prerequisite",
        )
        if key in diagnostics
    }
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
        "last_error": details.get("last_error"),
        "manager_error": details.get("manager_error"),
        "capability_readiness": details.get("capability_readiness"),
        "diagnostics": diagnostic_summary,
    }


def _resolve_workspace_root(workspace_root: Path | None) -> Path:
    if workspace_root is not None:
        return workspace_root.resolve()
    configured_root = os.getenv("PHYSICAL_AGENT_ROOT")
    if configured_root:
        return Path(configured_root).resolve()
    return Path(__file__).resolve().parents[3]


async def wait_for_provider_hot_readiness(
    manager: ManagerClient,
    provider_id: str,
    *,
    required_capability: str | None,
    timeout_s: float,
    poll_interval_s: float = 0.25,
    dependency_provider_ids: tuple[str, ...] = (),
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
        dependency_snapshots = [
            dependency_snapshot
            for dependency_id in dependency_provider_ids
            for dependency_snapshot in (
                _provider_readiness_snapshot(
                    next(
                        (
                            provider
                            for provider in providers
                            if str(provider.get("config", {}).get("id") or "")
                            == dependency_id
                        ),
                        None,
                    )
                ),
            )
            if dependency_snapshot is not None
        ]
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
        blocking_prerequisites = [
            {
                "provider_id": candidate.get("provider_id"),
                **candidate["diagnostics"]["blocking_prerequisite"],
            }
            for candidate in [snapshot, *dependency_snapshots]
            if isinstance(candidate, dict)
            and isinstance(candidate.get("diagnostics"), dict)
            and isinstance(
                candidate["diagnostics"].get("blocking_prerequisite"),
                dict,
            )
            and candidate["diagnostics"]["blocking_prerequisite"].get(
                "requires_external_action"
            )
            is True
        ]
        if blocking_prerequisites:
            return {
                "status": "BLOCKED_BY_PREREQUISITE",
                "provider_ready": provider_ready,
                "required_capability": required_capability,
                "capability_advertised": capability_advertised,
                "capability_ready": capability_ready,
                "provider": snapshot,
                "dependencies": dependency_snapshots,
                "capability": latest_capability,
                "advertised_capabilities": sorted(
                    str(capability.get("capability") or "")
                    for capability in advertised_capabilities
                    if capability.get("capability")
                ),
                "blocking_prerequisites": blocking_prerequisites,
                "timeout_s": timeout,
            }
        if provider_ready and capability_ready is not False:
            return {
                "status": "READY",
                "provider_ready": True,
                "required_capability": required_capability,
                "capability_advertised": capability_advertised,
                "capability_ready": capability_ready,
                "provider": snapshot,
                "dependencies": dependency_snapshots,
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
                "dependencies": dependency_snapshots,
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
        return False
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


def _deduplicating_agent_event_sink(event_sink: AgentEventSink) -> AgentEventSink:
    """Suppress duplicate visual payloads from live children and final tools."""

    visual_evidence_ids: set[str] = set()

    async def publish(event_type: str, payload: dict[str, Any]) -> None:
        if event_type == "visual.evidence.created":
            evidence_id = str(payload.get("evidence_id") or "").strip()
            if evidence_id and evidence_id in visual_evidence_ids:
                return
            if evidence_id:
                visual_evidence_ids.add(evidence_id)
        await event_sink(event_type, payload)

    return publish


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
    """Build the regulated complete-catalog fallback used by host tests."""

    compact_providers: list[dict[str, Any]] = []
    for provider in providers:
        config = provider.get("config")
        config = config if isinstance(config, dict) else {}
        report = provider.get("report")
        report = report if isinstance(report, dict) else {}
        details = report.get("details")
        details = details if isinstance(details, dict) else {}
        diagnostics = details.get("diagnostics")
        diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
        compact_providers.append(
            {
                "provider_id": config.get("id") or report.get("provider_id"),
                "display_name": config.get("display_name"),
                "dependencies": config.get("dependencies") or [],
                "process_state": provider.get("process_state"),
                "instance_id": report.get("instance_id"),
                "boot_id": report.get("boot_id"),
                "residency": report.get("residency"),
                "health": report.get("health"),
                "ready": bool(report.get("ready")),
                "expired": bool(report.get("expired")),
                "last_seen": report.get("last_seen"),
                "last_error": _bounded_runtime_catalog_value(
                    details.get("last_error")
                ),
                "manager_error": _bounded_runtime_catalog_value(
                    details.get("manager_error")
                ),
                "blocking_prerequisite": _bounded_runtime_catalog_value(
                    diagnostics.get("blocking_prerequisite")
                ),
                "detail_schema": "midbrain.manager.provider_detail.v1",
                "detail_sections": [
                    "/config",
                    "/process_state",
                    "/last_exit",
                    "/report",
                ],
            }
        )
    return redact_credential_values(
        {
            "schema": "midbrain.manager.agent_runtime_catalog",
            "schema_version": 1,
            "providers": compact_providers,
            "capabilities": [
                {
                    "capability": capability.get("capability"),
                    "provider_id": capability.get("provider_id"),
                    "provider_instance_id": capability.get(
                        "provider_instance_id"
                    ),
                    "available": bool(capability.get("available")),
                }
                for capability in capabilities
            ],
            "eligible_skill_tools": sorted(set(eligible_skill_tools)),
        }
    )


def _bounded_runtime_catalog_value(value: Any) -> Any:
    if value is None:
        return None
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    if len(encoded.encode("utf-8")) <= 2048:
        return copy.deepcopy(value)
    return {
        "truncated": True,
        "serialized_bytes": len(encoded.encode("utf-8")),
        "preview": encoded[:400],
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
        fabric_world_point_composer: (
            FabricWorldPointComposer | None
        ) = None,
        fabric_spatial_translator: FabricSpatialTranslator | None = None,
        scene_policy_publisher: (
            SceneSegmentationPolicyPublisher | None
        ) = None,
        tool_registration_skill: ToolControlFrameSkillAdapter | None = None,
        external_skill_adapters: (
            dict[str, SkillExecutionAdapter] | None
        ) = None,
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
        provider_hot_readiness_timeout_overrides_s: (
            Mapping[str, float] | None
        ) = None,
        provider_hot_readiness_poll_interval_s: float = 0.25,
        max_turns: int = 16,
        session_history_item_limit: int | None = None,
        skill_invocation_broker_handle: (
            SkillInvocationBrokerHandle | None
        ) = None,
        limited_graph_model_route_profiles: (
            Mapping[str, HostedModelRouteProfile] | None
        ) = None,
        skill_result_detail_store: SkillResultDetailStore | None = None,
    ):
        self.skill = skill
        self.integrated_motion_skill = integrated_motion_skill
        self.no_contact_approach_skill = no_contact_approach_skill
        self.skill_result_detail_store = skill_result_detail_store
        self._prepared_relative_motion: (
            CallScopedPreparedActionCoordinator | None
        ) = None
        self._prepared_world_point_motion: (
            CallScopedPreparedActionCoordinator | None
        ) = None
        self.max_turns = int(max_turns)
        if not 1 <= self.max_turns <= 32:
            raise ValueError("max_turns must be between 1 and 32")
        self.provider_hot_readiness_timeout_s = float(
            provider_hot_readiness_timeout_s
        )
        self.provider_hot_readiness_timeout_overrides_s: dict[str, float] = {}
        for provider_id, value in dict(
            provider_hot_readiness_timeout_overrides_s or {}
        ).items():
            normalized_provider_id = str(provider_id).strip()
            timeout = float(value)
            if not normalized_provider_id:
                raise ValueError(
                    "provider HOT readiness timeout override IDs must be non-empty"
                )
            if not math.isfinite(timeout) or timeout <= 0.0:
                raise ValueError(
                    "provider HOT readiness timeout overrides must be positive"
                )
            self.provider_hot_readiness_timeout_overrides_s[
                normalized_provider_id
            ] = timeout
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
        root = _resolve_workspace_root(workspace_root)
        eligible = set(
            eligible_tool_names or {"identify_pointed_object"}
        )
        descriptors = discover_agent_skills(root)
        integrated_motion_descriptor = next(
            (
                descriptor
                for descriptor in descriptors
                if descriptor.tool_name
                == PERFORM_RELATIVE_EFFECTOR_MOTION_TOOL
            ),
            None,
        )
        world_point_motion_descriptor = next(
            (
                descriptor
                for descriptor in descriptors
                if descriptor.tool_name
                == MOVE_EFFECTOR_TO_WORLD_POINT_TOOL
            ),
            None,
        )

        async def identify_adapter(arguments: dict[str, Any]) -> str:
            question = arguments.get("question")
            if not isinstance(question, str) or not question.strip():
                raise ValueError("question must be non-empty text")
            return await self.skill.run(question)

        adapters: dict[str, SkillExecutionAdapter] = {
            "test_agent.identify_pointed_object.v1": BoundMethodSkillAdapter(
                identify_adapter
            ),
        }
        for adapter_id, adapter in (external_skill_adapters or {}).items():
            if adapter_id in adapters:
                raise ValueError(
                    f"duplicate Skill execution adapter: {adapter_id}"
                )
            adapters[adapter_id] = adapter
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
                        0.0,
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
        if fabric_world_point_composer is not None:
            async def fabric_world_point_adapter(
                arguments: dict[str, Any],
            ) -> str:
                result = await fabric_world_point_composer.run(
                    object_id=arguments.get("object_id"),
                    corner_name=arguments.get("corner_name"),
                    offset_vector=arguments.get("offset_vector"),
                    offset_unit=arguments.get("offset_unit"),
                    offset_reference=arguments.get("offset_reference"),
                    expected_scene_revision=arguments.get(
                        "expected_scene_revision"
                    ),
                )
                return json.dumps(result, ensure_ascii=False, default=str)

            adapters[
                "skill.derive_fabric_world_point.v1"
            ] = BoundMethodSkillAdapter(fabric_world_point_adapter)
            eligible.add(DERIVE_FABRIC_WORLD_POINT_TOOL)
        if fabric_spatial_translator is not None:
            async def fabric_direction_translation_adapter(
                arguments: dict[str, Any],
            ) -> str:
                result = await fabric_spatial_translator.translate_direction(
                    direction=arguments.get("direction"),
                    source_reference=arguments.get("source_reference"),
                    source_frame_id=arguments.get("source_frame_id"),
                    source_observed_at_us=arguments.get(
                        "source_observed_at_us"
                    ),
                    source_session_epoch=arguments.get(
                        "source_session_epoch"
                    ),
                )
                return json.dumps(result, ensure_ascii=False, default=str)

            async def fabric_pose_translation_adapter(
                arguments: dict[str, Any],
            ) -> str:
                result = await fabric_spatial_translator.translate_pose(
                    position_m=arguments.get("position_m"),
                    orientation_xyzw=arguments.get("orientation_xyzw"),
                    source_reference=arguments.get("source_reference"),
                    source_frame_id=arguments.get("source_frame_id"),
                    source_observed_at_us=arguments.get(
                        "source_observed_at_us"
                    ),
                    source_session_epoch=arguments.get(
                        "source_session_epoch"
                    ),
                )
                return json.dumps(result, ensure_ascii=False, default=str)

            async def world_point_offset_adapter(
                arguments: dict[str, Any],
            ) -> str:
                result = await fabric_spatial_translator.offset_world_point(
                    source_position_world_m=arguments.get(
                        "source_position_world_m"
                    ),
                    source_world_frame_id=arguments.get(
                        "source_world_frame_id"
                    ),
                    source_observed_at_us=arguments.get(
                        "source_observed_at_us"
                    ),
                    source_session_epoch=arguments.get(
                        "source_session_epoch"
                    ),
                    offset_vector=arguments.get("offset_vector"),
                    offset_unit=arguments.get("offset_unit"),
                    offset_reference=arguments.get("offset_reference"),
                )
                return json.dumps(result, ensure_ascii=False, default=str)

            adapters[
                "skill.translate_fabric_direction.v1"
            ] = BoundMethodSkillAdapter(fabric_direction_translation_adapter)
            adapters[
                "skill.translate_fabric_pose.v1"
            ] = BoundMethodSkillAdapter(fabric_pose_translation_adapter)
            adapters["skill.offset_world_point.v1"] = BoundMethodSkillAdapter(
                world_point_offset_adapter
            )
            eligible.add(TRANSLATE_FABRIC_DIRECTION_TOOL)
            eligible.add(TRANSLATE_FABRIC_POSE_TOOL)
            eligible.add(OFFSET_WORLD_POINT_TOOL)
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
            async def prepare_integrated_relative_motion(
                arguments: dict[str, Any],
            ) -> dict[str, Any]:
                return await integrated_motion_skill.preview(
                    direction=arguments.get("direction"),
                    distance_m=arguments.get("distance_m"),
                    translation_vector_m=arguments.get(
                        "translation_vector_m"
                    ),
                    requested_speed_m_s=arguments.get(
                        "requested_speed_m_s"
                    ),
                    execution_backend=arguments.get(
                        "execution_backend", "IMPEDANCE"
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
                    controlled_frame_rpy_delta_deg=arguments.get(
                        "controlled_frame_rpy_delta_deg"
                    ),
                    target_orientation_rpy_rad=arguments.get(
                        "target_orientation_rpy_rad"
                    ),
                )
            async def prepare_integrated_world_point_motion(
                arguments: dict[str, Any],
            ) -> dict[str, Any]:
                return await integrated_motion_skill.preview_world_point(
                    target_position_world_m=arguments.get(
                        "target_position_world_m"
                    ),
                    target_world_frame_id=arguments.get(
                        "target_world_frame_id"
                    ),
                    target_session_epoch=arguments.get(
                        "target_session_epoch"
                    ),
                    requested_speed_m_s=arguments.get(
                        "requested_speed_m_s"
                    ),
                    execution_backend=arguments.get(
                        "execution_backend", "IMPEDANCE"
                    ),
                )

            if integrated_motion_descriptor is None:
                raise RuntimeError(
                    "integrated relative motion Skill descriptor is unavailable"
                )
            if world_point_motion_descriptor is None:
                raise RuntimeError(
                    "absolute world-point motion Skill descriptor is unavailable"
                )

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
            detail_store=skill_result_detail_store,
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
        if skill_result_detail_store is not None:
            async def inspect_skill_result_detail(
                _context,
                raw_arguments: str,
            ) -> str:
                arguments = json.loads(raw_arguments)
                result_id = arguments.get("result_id")
                pointer = arguments.get("json_pointer")
                if not isinstance(result_id, str) or not result_id.strip():
                    raise ValueError("result_id must be non-empty text")
                if pointer is not None and (
                    not isinstance(pointer, str) or not pointer.startswith("/")
                ):
                    raise ValueError("json_pointer must be null or start with /")
                record = await skill_result_detail_store.retrieve(result_id)
                if record is None:
                    raise ValueError("Skill result detail is unavailable or expired")
                selected = select_result_detail(record, pointer)
                return json.dumps(selected, ensure_ascii=False, default=str)

            offered_tools.append(
                FunctionTool(
                    name="inspect_skill_result_detail",
                    description=(
                        "Read the complete sanitized output, or one exact JSON "
                        "pointer, from a prior Skill invocation's opaque "
                        "detail_ref. Use only when compact fields are "
                        "insufficient. This is a top-level diagnostic "
                        "observation tool, not a Skill or graph child, and it "
                        "cannot authorize or repeat an action. Use null for "
                        "json_pointer to request the full sanitized output."
                    ),
                    params_json_schema={
                        "type": "object",
                        "properties": {
                            "result_id": {"type": "string", "minLength": 1},
                            "json_pointer": {"type": ["string", "null"]},
                        },
                        "required": ["result_id", "json_pointer"],
                        "additionalProperties": False,
                    },
                    on_invoke_tool=inspect_skill_result_detail,
                    strict_json_schema=True,
                    needs_approval=False,
                )
            )
        if defer_loading:
            offered_tools.append(ToolSearchTool())
        if scene_policy_publisher is not None and not (
            developer_mode or provider_lifecycle_control
        ):
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
                    params_json_schema=_scene_policy_parameters_schema(),
                    on_invoke_tool=configure_scene_segmentation_policy,
                    strict_json_schema=True,
                    needs_approval=False,
                )
            )
        if no_contact_approach_skill is not None:
            async def execute_no_contact_approach_step(
                _context,
                _raw_arguments: str,
            ) -> str:
                result = await no_contact_approach_skill.execute_current_preview()
                return json.dumps(result, ensure_ascii=False, default=str)

            offered_tools.append(
                FunctionTool(
                    name="execute_no_contact_approach_step",
                    description=(
                        "Execute one exact, fresh, collision-checked no-contact "
                        "item-approach preview created in this Agent turn. All motion "
                        "parameters and controller digests are recovered from "
                        "host state rather than copied or selected by the model. "
                        "The autonomous free-space policy immediately requests "
                        "one bounded physical correction of at most the planned step, after "
                        "which both item and effector must be observed again. "
                        "WAITING_NEXT and HOLDING_FINAL are measured-arrival "
                        "terminal states, not incomplete motion, when the result "
                        "sets measured_arrival_confirmed=true."
                    ),
                    params_json_schema={
                        "type": "object",
                        "properties": {},
                        "required": [],
                        "additionalProperties": False,
                    },
                    on_invoke_tool=execute_no_contact_approach_step,
                    strict_json_schema=True,
                    needs_approval=False,
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
                catalog = redact_credential_values(
                    await manager.agent_runtime_catalog()
                )
                catalog["eligible_skill_tools"] = sorted(
                    descriptor.tool_name
                    for descriptor in self.offered_skill_descriptors
                )
                return json.dumps(
                    catalog,
                    ensure_ascii=False,
                    default=str,
                )

            async def configure_scene_policy_and_inspect_runtime(
                _context,
                raw_arguments: str,
            ) -> str:
                if scene_policy_publisher is None:
                    raise RuntimeError(
                        "combined scene setup requires a scene-policy publisher"
                    )
                arguments = json.loads(raw_arguments)
                policy_result = await scene_policy_publisher.publish_policy(
                    policy_id=arguments.get("policy_id"),
                    objects=arguments.get("objects"),
                    arm_description=arguments.get("arm_description"),
                )
                catalog = redact_credential_values(
                    await manager.agent_runtime_catalog()
                )
                catalog["eligible_skill_tools"] = sorted(
                    descriptor.tool_name
                    for descriptor in self.offered_skill_descriptors
                )
                return json.dumps(
                    {
                        "schema": "midbrain.agent.scene_policy_runtime_setup",
                        "schema_version": 1,
                        "scene_policy": policy_result,
                        "runtime_catalog": catalog,
                        "agent_instruction": (
                            "Select the exact configured Provider and capability "
                            "from runtime_catalog, then call "
                            "set_provider_residency once. This tool did not "
                            "change Provider lifecycle state or grant authority."
                        ),
                    },
                    ensure_ascii=False,
                    default=str,
                    separators=(",", ":"),
                )

            async def inspect_provider_detail(
                _context,
                raw_arguments: str,
            ) -> str:
                arguments = json.loads(raw_arguments)
                provider_id = arguments.get("provider_id")
                pointer = arguments.get("json_pointer")
                if not isinstance(provider_id, str) or not provider_id.strip():
                    raise ValueError("provider_id must be non-empty text")
                if pointer is not None and (
                    not isinstance(pointer, str) or not pointer.startswith("/")
                ):
                    raise ValueError("json_pointer must be null or start with /")
                detail = redact_credential_values(
                    await manager.provider_detail(provider_id.strip())
                )
                selected = select_json_pointer(detail, pointer)
                return json.dumps(
                    {
                        "schema": "midbrain.manager.provider_detail_observation",
                        "schema_version": 1,
                        "provider_id": provider_id.strip(),
                        "selected_pointer": pointer,
                        "detail": selected,
                    },
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
                    dependency_provider_ids = tuple(
                        str(value).strip()
                        for value in result.get("manager_hot_dependencies", [])
                        if str(value).strip()
                    )
                    readiness = await wait_for_provider_hot_readiness(
                        manager,
                        provider_id,
                        required_capability=required_capability,
                        timeout_s=self.provider_hot_readiness_timeout_overrides_s.get(
                            provider_id,
                            self.provider_hot_readiness_timeout_s,
                        ),
                        poll_interval_s=(
                            self.provider_hot_readiness_poll_interval_s
                        ),
                        dependency_provider_ids=dependency_provider_ids,
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
                elif (
                    readiness is not None
                    and readiness.get("status") == "BLOCKED_BY_PREREQUISITE"
                ):
                    agent_instruction = (
                        f"Manager accepted the {action.upper()} request, but "
                        "structured Provider evidence reports an external "
                        "prerequisite. Do not wait for the timeout or retry "
                        "this lifecycle transition. Report the exact "
                        "blocking_prerequisites. A read-only diagnostic Skill "
                        "may be called once if the original task requires its "
                        "typed result."
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
                            "Inspect the regulated current Manager catalog for "
                            "every configured Provider and every advertised "
                            "capability, including compact lifecycle, identity, "
                            "dependency, readiness, expiry, and blocking-error "
                            "state plus eligible finite Skill names. Use "
                            "inspect_provider_detail only when this compact "
                            "complete catalog is insufficient. This operation "
                            "is read-only."
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
                        name="inspect_provider_detail",
                        description=(
                            "Read the complete sanitized current Manager "
                            "record, or one exact JSON pointer, for one "
                            "Provider ID from inspect_midbrain_runtime. This "
                            "is a top-level diagnostic observation tool, not "
                            "a Skill, lifecycle command, or graph child. Use "
                            "null for json_pointer to request the full "
                            "sanitized Provider record."
                        ),
                        params_json_schema={
                            "type": "object",
                            "properties": {
                                "provider_id": {
                                    "type": "string",
                                    "minLength": 1,
                                },
                                "json_pointer": {
                                    "type": ["string", "null"],
                                },
                            },
                            "required": ["provider_id", "json_pointer"],
                            "additionalProperties": False,
                        },
                        on_invoke_tool=inspect_provider_detail,
                        strict_json_schema=True,
                        needs_approval=False,
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
            if scene_policy_publisher is not None:
                offered_tools.append(
                    FunctionTool(
                        name=CONFIGURE_SCENE_POLICY_AND_INSPECT_RUNTIME_TOOL,
                        description=(
                            "Publish the user's explicit scene segmentation "
                            "policy to Fabric and return the fresh regulated "
                            "Manager catalog in the same host call. This combines "
                            "one policy write with one read-only observation; it "
                            "does not start, warm, authorize, or invoke a "
                            "Provider. Use the returned exact Provider and "
                            "capability in a separate set_provider_residency "
                            "call."
                        ),
                        params_json_schema=_scene_policy_parameters_schema(),
                        on_invoke_tool=(
                            configure_scene_policy_and_inspect_runtime
                        ),
                        strict_json_schema=True,
                        needs_approval=False,
                    )
                )
        if integrated_motion_skill is not None:
            async def prepare_relative_motion_action(
                arguments: dict[str, Any],
            ) -> dict[str, Any]:
                try:
                    normalized_arguments = dict(arguments)
                    for optional_field in (
                        "controlled_frame_rpy_delta_deg",
                        "execution_backend",
                        "target_orientation_rpy_rad",
                        "translation_vector_m",
                    ):
                        normalized_arguments.setdefault(
                            optional_field,
                            "IMPEDANCE"
                            if optional_field == "execution_backend"
                            else None,
                        )
                    validate(
                        instance=normalized_arguments,
                        schema=integrated_motion_descriptor.input_schema,
                    )
                    extend_current_operation_hard_timeout(
                        float(adapter_timeout_s),
                        stage=(
                            "skill:perform_relative_effector_motion:preparing"
                        ),
                    )
                    return await asyncio.wait_for(
                        prepare_integrated_relative_motion(
                            normalized_arguments
                        ),
                        timeout=float(adapter_timeout_s),
                    )
                except Exception as error:
                    logger.exception(
                        "Integrated relative motion preparation failed"
                    )
                    return {
                        "status": "MOTION_PREPARATION_FAILED",
                        "workflow_complete": False,
                        "physical_motion_authorized": False,
                        "physical_motion_submitted": False,
                        "message": str(error),
                    }

            def select_relative_motion_continuation(
                result: dict[str, Any],
            ) -> dict[str, Any] | None:
                if (
                    result.get("status") != "PREVIEW_READY"
                    or result.get("workflow_complete") is not False
                    or result.get("physical_motion_authorized") is not False
                ):
                    return None
                continuation = result.get("required_next_tool")
                if not isinstance(continuation, dict) or (
                    continuation.get("name")
                    != "HOST_INTERNAL_SIGNED_PATH_COMMIT"
                ):
                    return None
                arguments = continuation.get("arguments")
                if not isinstance(arguments, dict) or set(arguments) != {
                    "preview_id"
                }:
                    return None
                preview_id = arguments.get("preview_id")
                if not isinstance(preview_id, str) or not preview_id.strip():
                    return None
                return {"preview_id": preview_id.strip()}

            async def resolve_relative_motion_authorization(
                arguments: dict[str, Any],
            ) -> dict[str, Any] | None:
                preview_id = arguments.get("preview_id")
                canonical = await (
                    integrated_motion_skill
                    .pending_execution_authorization_arguments(preview_id)
                )
                if not isinstance(canonical, dict) or (
                    canonical.get("preview_id") != preview_id
                ):
                    return None
                return canonical

            async def execute_relative_motion_continuation(
                arguments: dict[str, Any],
            ) -> dict[str, Any]:
                return await integrated_motion_skill.execute_preview(
                    preview_id=arguments.get("preview_id"),
                )

            self._prepared_relative_motion = (
                CallScopedPreparedActionCoordinator(
                    prepare_action=prepare_relative_motion_action,
                    select_continuation=(
                        select_relative_motion_continuation
                    ),
                    resolve_authorization=(
                        resolve_relative_motion_authorization
                    ),
                    execute_continuation=(
                        execute_relative_motion_continuation
                    ),
                )
            )

            async def perform_relative_effector_motion(
                context_wrapper: Any,
                raw_arguments: str,
            ) -> str:
                arguments = json.loads(raw_arguments)
                assert self._prepared_relative_motion is not None
                call_id = getattr(context_wrapper, "tool_call_id", "")
                await self._prepared_relative_motion.prepare_for_call(
                    call_id,
                    arguments,
                )
                result = await self._prepared_relative_motion.execute_for_call(
                    call_id,
                    arguments,
                )
                compact_result = await finalize_skill_result(
                    result,
                    integrated_motion_descriptor,
                    skill_result_detail_store,
                )
                return json.dumps(
                    compact_result,
                    ensure_ascii=False,
                    default=str,
                )

            eligible.add(PERFORM_RELATIVE_EFFECTOR_MOTION_TOOL)
            if integrated_motion_descriptor not in self.offered_skill_descriptors:
                self.offered_skill_descriptors.append(
                    integrated_motion_descriptor
                )
            offered_tools.append(
                FunctionTool(
                        name=PERFORM_RELATIVE_EFFECTOR_MOTION_TOOL,
                        description=(
                            "Prepare, authorize, and execute one requested "
                            "relative end-effector translation, bounded "
                            "controlled-frame rotation, absolute arm-base "
                            "orientation, or combined pose change "
                            "as one call-scoped finite action. The host first "
                            "creates a nonphysical preview. Only PREVIEW_READY "
                            "with the exact opaque execution continuation can "
                            "reach the autonomous free-space policy and "
                            "controller commit. Dependency, alignment, "
                            "confirmation, preview, authorization, freshness, "
                            "and controller failures return without motion. "
                            "Declared structured-result pointers for "
                            "composition: "
                            f"{describe_output_schema(integrated_motion_descriptor.output_schema)}."
                        ),
                        params_json_schema=integrated_motion_descriptor.input_schema,
                        on_invoke_tool=perform_relative_effector_motion,
                        strict_json_schema=True,
                        needs_approval=False,
                )
            )

            async def prepare_world_point_motion_action(
                arguments: dict[str, Any],
            ) -> dict[str, Any]:
                try:
                    normalized_arguments = dict(arguments)
                    for optional_field in (
                        "target_world_frame_id",
                        "target_session_epoch",
                        "requested_speed_m_s",
                        "execution_backend",
                    ):
                        normalized_arguments.setdefault(
                            optional_field,
                            "IMPEDANCE"
                            if optional_field == "execution_backend"
                            else None,
                        )
                    validate(
                        instance=normalized_arguments,
                        schema=world_point_motion_descriptor.input_schema,
                    )
                    extend_current_operation_hard_timeout(
                        float(adapter_timeout_s),
                        stage=(
                            "skill:move_effector_to_world_point:preparing"
                        ),
                    )
                    return await asyncio.wait_for(
                        prepare_integrated_world_point_motion(
                            normalized_arguments
                        ),
                        timeout=float(adapter_timeout_s),
                    )
                except Exception as error:
                    logger.exception(
                        "Absolute world-point motion preparation failed"
                    )
                    return {
                        "status": "MOTION_PREPARATION_FAILED",
                        "workflow_complete": False,
                        "physical_motion_authorized": False,
                        "physical_motion_submitted": False,
                        "message": str(error),
                    }

            self._prepared_world_point_motion = (
                CallScopedPreparedActionCoordinator(
                    prepare_action=prepare_world_point_motion_action,
                    select_continuation=(
                        select_relative_motion_continuation
                    ),
                    resolve_authorization=(
                        resolve_relative_motion_authorization
                    ),
                    execute_continuation=(
                        execute_relative_motion_continuation
                    ),
                )
            )

            async def move_effector_to_world_point(
                context_wrapper: Any,
                raw_arguments: str,
            ) -> str:
                arguments = json.loads(raw_arguments)
                assert self._prepared_world_point_motion is not None
                call_id = getattr(context_wrapper, "tool_call_id", "")
                await self._prepared_world_point_motion.prepare_for_call(
                    call_id,
                    arguments,
                )
                result = (
                    await self._prepared_world_point_motion.execute_for_call(
                        call_id,
                        arguments,
                    )
                )
                compact_result = await finalize_skill_result(
                    result,
                    world_point_motion_descriptor,
                    skill_result_detail_store,
                )
                return json.dumps(
                    compact_result,
                    ensure_ascii=False,
                    default=str,
                )

            eligible.add(MOVE_EFFECTOR_TO_WORLD_POINT_TOOL)
            if (
                world_point_motion_descriptor
                not in self.offered_skill_descriptors
            ):
                self.offered_skill_descriptors.append(
                    world_point_motion_descriptor
                )
            offered_tools.append(
                FunctionTool(
                    name=MOVE_EFFECTOR_TO_WORLD_POINT_TOOL,
                    description=(
                        "Prepare, authorize, and execute one exact absolute "
                        "world-coordinate free-space move of the controlled "
                        "effector. The host converts the point through the "
                        "current reviewed world-to-arm transform, preserves "
                        "the measured effector orientation with POSE_6DOF IK, "
                        "and binds the signed preview continuation to this "
                        "call. Frame, epoch, transform, collision, IK, "
                        "authorization, and dependency failures return "
                        "without motion. Declared structured-result pointers "
                        "for composition: "
                        f"{describe_output_schema(world_point_motion_descriptor.output_schema)}."
                    ),
                    params_json_schema=(
                        world_point_motion_descriptor.input_schema
                    ),
                    on_invoke_tool=move_effector_to_world_point,
                    strict_json_schema=True,
                    needs_approval=False,
                )
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
                "Its compact snapshot includes every Provider and capability "
                "with regulated lifecycle and readiness fields. Use "
                "inspect_provider_detail for the complete sanitized record "
                "of one exact Provider only when needed. "
                "When a Provider lifecycle transition is necessary, call "
                "set_provider_residency immediately. Never answer by asking "
                "for conversational permission. The host autonomously permits "
                "task-required start, hot, and warm transitions; stop remains "
                "subject to the host lifecycle policy. A rejected lifecycle "
                "transition is final for the current run: do not request "
                "the identical transition again. After an accepted lifecycle "
                "call, do not request that identical transition again in the "
                "same run; inspect once and report nonconvergence if the "
                "runtime still does not satisfy the dependency. For a "
                "requested physical relative effector motion, call "
                "perform_relative_effector_motion directly instead of "
                "inspecting or activating Providers preemptively. The host "
                "prepares a nonphysical preview inside that exact SDK call, "
                "binds its opaque continuation to the call ID, resolves the "
                "exact signed free-space policy autonomously, and only then "
                "executes it. If the "
                "result reports a dependency unavailable, follow its typed "
                "required_next_tool once. Request only the Integrated "
                "capability named by the continuation; Manager owns selection "
                "and transitive activation of its declared Basic dependency. "
                "Then retry perform_relative_effector_motion once with the "
                "original semantic request. Use "
                "No human approval is requested for signed non-contact "
                "motion. Treat reach, touch, until reaching, and until touching "
                "as no-contact boundary targets unless the operator explicitly "
                "requests sustained force work such as pushing, pressing, "
                "cutting, scraping, or gripping. Such endpoint wording is "
                "neither contact authorization nor a reason to refuse motion. "
                "Integrated permits zero extra WORK_OBJECT clearance without "
                "intersection and preserves 10 mm from KEEP_OUT obstacles. If "
                "the controller returns closest-safe, report the successful "
                "no-contact boundary result and do not retry through the "
                "blocking object. Intentional force/contact work requires "
                "another Skill. "
                "Report success only when the tool returns "
                "physical_motion_completed=true; otherwise report its exact "
                "unsuccessful or unconfirmed completion outcome. "
                "Treat every relative motion request as a new displacement "
                "from the current measured pose, including repeated requests. "
                "This relative-pose Skill uses the Integrated Controller's "
                "signed waypoint path over Basic MIT joint commands and ends "
                "in gravity float, so an explicit MIT-mode request needs no "
                "additional mode field. For legacy yaw, use "
                "APPLY_CONTROLLED_FRAME_YAW_DELTA. "
                "For controlled-frame-relative 3D rotation, use APPLY_CONTROLLED_FRAME_RPY_DELTA "
                "with [roll, pitch, yaw] degrees. For an absolute attitude, use "
                "SET_ARM_BASE_RPY with [roll, pitch, yaw] radians. Pure rotation "
                "uses direction=NONE and distance_m=0. Multi-axis displacement "
                "uses translation_vector_m=[x,y,z] in reference_frame and omits "
                "direction and distance_m. The "
                "phrase arm forward explicitly maps to "
                "direction=ARM_BASE_POSITIVE_X with reference_frame=ARM_BASE. "
                "Never reject a combined 3D translation and orientation merely "
                "because a legacy single-axis or yaw-only form cannot express it. "
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
                    " For an explicit Provider lifecycle request, inspect the "
                    "runtime and call set_provider_residency for the necessary "
                    "transition. When a Skill reports a cold dependency, "
                    "follow its typed required_next_tool directly and let "
                    "Manager resolve declared transitive dependencies; inspect "
                    "only if that transition does not converge. Do this instead "
                    "of answering with a "
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
                    " For a requested physical relative end-effector motion, "
                    "call perform_relative_effector_motion directly; do not "
                    "inspect or activate Providers preemptively. The host "
                    "creates the nonphysical Integrated IK preview inside the "
                    "same call, binds its opaque continuation to the exact SDK "
                    "call ID, evaluates the autonomous free-space policy, and "
                    "only then commits that preview. If the result reports a "
                    "cold dependency, follow its typed required_next_tool once "
                    "and request only its Integrated capability. Manager owns "
                    "selection and transitive activation of the declared Basic "
                    "dependency. After that transition is ready, retry "
                    "perform_relative_effector_motion once with the original "
                    "semantic request. "
                    "If Integrated is stopped, cold, or requires HOT "
                    "recovery, do not report that motion approval is needed and end "
                    "the run: call set_provider_residency immediately so its "
                    "lifecycle policy can be resolved. Free-space motion tools "
                    "must not request human approval. "
                    "A PREVIEW_READY result from an explicitly requested raw "
                    "preview remains nonphysical and must not be described as "
                    "completed motion. "
                    "If preview reports DEPENDENCY_UNAVAILABLE, follow its "
                    "required_next_tool and activation sequence; do not repeat "
                    "the same preview call while the controller is unreachable. "
                    "If preview reports INTEGRATED_RECOVERY_REQUIRED, call "
                    "its required_next_tool unchanged and request the explicit "
                    "approved HOT transition. Do this even when Manager says "
                    "the provider process is already running: HOT is the "
                    "controller recovery boundary that reacquires its Basic "
                    "lease. After approval, retry the prepared-action tool. "
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
                    "position-only IK. For a legacy left/right turn, use "
                    "APPLY_CONTROLLED_FRAME_YAW_DELTA. For general controlled-frame-relative "
                    "roll/pitch/yaw, use APPLY_CONTROLLED_FRAME_RPY_DELTA. For "
                    "an absolute arm-base attitude, use SET_ARM_BASE_RPY. Pure "
                    "rotation uses direction=NONE, distance_m=0, and no requested "
                    "speed. A single multi-axis displacement uses "
                    "translation_vector_m and omits direction and distance_m; "
                    "never split it into sequential axis moves. Explicit arm "
                    "forward maps to "
                    "direction=ARM_BASE_POSITIVE_X and "
                    "reference_frame=ARM_BASE. This Skill uses the Integrated "
                    "Controller's signed waypoint path over Basic MIT joint "
                    "commands, so an explicit MIT-mode request is supported "
                    "without a separate mode argument. When the operator specifies a motion "
                    "speed, pass it as requested_speed_m_s. The adapter "
                    "passes it to the controller-owned path scheduler. "
                    "Describe it as nominal average endpoint speed, not "
                    "constant Cartesian velocity, and report any longer "
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
                    "Report success only when the tool "
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
                    " FoundationPose is retained as a slow explicitly named "
                    "initializer, not as the default world-to-arm alignment "
                    "route. Call calibrate_stationary_workcell whenever the "
                    "operator's request mentions FoundationPose by name. The "
                    "name match is case-insensitive and accepts spacing, "
                    "hyphenation, and minor spelling errors. Pass the complete "
                    "operator request unchanged as the request argument. For "
                    "every other establish, calibrate, "
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
                    "another explicit FoundationPose request, call "
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
        if integrated_motion_skill is not None:
            instructions += (
                " When an absolute world XYZ target is already known, call "
                "move_effector_to_world_point directly. Never inspect runtime "
                "state or calculate a relative displacement to reach that "
                "point. Copy a source result's world-frame ID and VIO session "
                "epoch exactly when present; use null only for coordinates the "
                "operator intentionally states in the current world. This "
                "Skill always preserves the measured controlled-effector "
                "orientation with POSE_6DOF IK. In a compound workflow such "
                "as moving above a slicing start and then cutting, begin the "
                "contact action only after the world-point tool returns "
                "physical_motion_completed=true. A frame mismatch, epoch "
                "mismatch, transform failure, preview rejection, or dependency "
                "failure is not a completed move."
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
                "explicit approved Integrated HOT recovery and retry "
                "perform_relative_effector_motion instead of treating "
                "recovery as a terminal failure."
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
        if fabric_spatial_translator is not None:
            instructions += (
                " Coordinate-frame conversion is a read-only tandem "
                "operation, not language-model arithmetic. When a downstream "
                "Skill requires a world direction but the requested vector is "
                "in arm-base or controlled-effector axes, call "
                "translate_fabric_direction_to_world and copy its "
                "direction_world output unchanged into the downstream field "
                "with the same semantic role. Do not confuse coordinate type "
                "with role: a translated slicing direction remains a slicing "
                "direction, and a translated blade direction remains a blade "
                "direction. Use translate_fabric_pose_to_world only when a "
                "complete metric position plus XYZW orientation must be "
                "expressed in the active world. Its output is coordinate "
                "evidence and never authorizes or submits motion. Do not "
                "refuse solely because a supported frame conversion is "
                "required; invoke the appropriate translator and evaluate its "
                "typed result. When a downstream world point is defined as a "
                "metric offset from an earlier structured world point, call "
                "offset_world_point with the earlier point and frame identity "
                "unchanged. Never perform that addition in the model or "
                "substitute a nearby field with different semantics."
            )
        if "run_limited_graph" in eligible:
            instructions += LIMITED_GRAPH_AGENT_GUIDANCE
        if skill_invocation_broker_handle is not None:
            skill_invocation_broker_handle.bind(
                HostedSkillInvocationBroker(
                    self.offered_skill_descriptors,
                    offered_tools,
                    model_route_profiles=(
                        limited_graph_model_route_profiles
                    ),
                )
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
            approvals = []
            for item in result.interruptions:
                approvals.append(
                    await self._approval_description_with_pending(item)
                )
            return InteractiveAgentResult(
                answer=None,
                state=result.to_state(),
                approvals=approvals,
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
        active_event_sink = (
            _deduplicating_agent_event_sink(event_sink)
            if event_sink is not None
            else None
        )
        child_event_token = set_hosted_child_event_sink(active_event_sink)
        graph_repair_token = set_graph_authoring_repair_state(
            maximum_corrections=1
        )
        try:
            if (
                not isinstance(input_value, RunState)
                and getattr(self, "no_contact_approach_skill", None) is not None
            ):
                await self.no_contact_approach_skill.begin_agent_turn()
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
                routed_tools = _select_routed_tools(
                    self.agent.tools,
                    allowed_tools,
                    include_limited_graph=intent_route.get(
                        "allow_limited_graph",
                        True,
                    ),
                )
                routed_graph_reminder = (
                    LIMITED_GRAPH_ROUTED_REMINDER
                    if any(
                        getattr(tool, "name", "") == "run_limited_graph"
                        for tool in routed_tools
                    )
                    else ""
                )
                selected_agent = self.agent.clone(
                    tools=routed_tools,
                    instructions=(
                        f"{self.agent.instructions} "
                        f"Deterministic intent route "
                        f"{intent_route['route']}: "
                        f"{intent_route['instruction']}"
                        f"{routed_graph_reminder}"
                    ),
                )
            if active_event_sink is None:
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
                    active_event_sink,
                )
            result = await await_with_progress_heartbeat(
                awaitable,
                stage="AGENT_MODEL_AWAITING_RESPONSE",
            )
        finally:
            reset_graph_authoring_repair_state(graph_repair_token)
            reset_hosted_child_event_sink(child_event_token)
            reset_vlm_model_selection(vlm_token)
        report_operation_progress("AGENT_RUN_COMPLETED")
        return result

    @staticmethod
    def _final_output(output: Any) -> str:
        if isinstance(output, str):
            return output
        return json.dumps(output, ensure_ascii=False, default=str)

    @staticmethod
    def _approval_description(
        item: Any,
        *,
        canonical_motion_arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
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

        if canonical_motion_arguments is not None:
            arguments = dict(canonical_motion_arguments)

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
        elif tool_name == PERFORM_RELATIVE_EFFECTOR_MOTION_TOOL:
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
            orientation_change_text = yaw_text
            raw_rpy_delta = arguments.get("controlled_frame_rpy_delta_deg")
            if isinstance(raw_rpy_delta, list) and len(raw_rpy_delta) == 3:
                orientation_change_text = (
                    "controlled-frame RPY delta "
                    + ", ".join(f"{float(value):g}°" for value in raw_rpy_delta)
                )
            raw_absolute_rpy = arguments.get("target_orientation_rpy_rad")
            if (
                orientation_policy == "SET_ARM_BASE_RPY"
                and isinstance(raw_absolute_rpy, list)
                and len(raw_absolute_rpy) == 3
            ):
                orientation_change_text = (
                    "arm-base target RPY "
                    + ", ".join(
                        f"{float(value):g} rad" for value in raw_absolute_rpy
                    )
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
                title = f"Rotate the effector to {orientation_change_text}?"
            elif motion_intent == "NEW_RELATIVE_POSE_MOVE":
                title = (
                    f"Move the arm {direction} by {distance_text} and "
                    f"apply {orientation_change_text}?"
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
                "commanded orientation."
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
                {
                    "label": "Orientation change",
                    "value": orientation_change_text,
                },
                {
                    "label": "Orientation",
                    "value": (
                        "Preserve measured controlled-frame 3D orientation"
                        if orientation_policy
                        == "PRESERVE_MEASURED_CONTROLLED_FRAME"
                        else "Apply bounded controlled-frame yaw with POSE_6DOF"
                        if orientation_policy
                        == "APPLY_CONTROLLED_FRAME_YAW_DELTA"
                        else "Apply controlled-frame RPY delta with POSE_6DOF"
                        if orientation_policy
                        == "APPLY_CONTROLLED_FRAME_RPY_DELTA"
                        else "Set absolute arm-base RPY with POSE_6DOF"
                        if orientation_policy == "SET_ARM_BASE_RPY"
                        else "Position-only IK"
                    ),
                },
                {
                    "label": "Trigger",
                    "value": "Autonomous signed free-space path commit",
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
            "authorization_arguments": arguments,
        }

    async def _approval_description_with_pending(
        self,
        item: Any,
    ) -> dict[str, Any]:
        canonical: dict[str, Any] | None = None
        raw_item = item.raw_item
        if hasattr(raw_item, "model_dump"):
            raw = raw_item.model_dump(mode="json")
        elif isinstance(raw_item, dict):
            raw = raw_item
        else:
            raw = {
                "call_id": getattr(raw_item, "call_id", None),
                "arguments": getattr(raw_item, "arguments", {}),
            }
        if (
            item.tool_name == PERFORM_RELATIVE_EFFECTOR_MOTION_TOOL
            and self._prepared_relative_motion is not None
        ):
            canonical = await (
                self._prepared_relative_motion
                .authorization_arguments_for_call(raw.get("call_id"))
            )
        return self._approval_description(
            item,
            canonical_motion_arguments=canonical,
        )

    async def discard_pending_prepared_action(self, item: Any) -> None:
        if (
            item.tool_name != PERFORM_RELATIVE_EFFECTOR_MOTION_TOOL
            or self._prepared_relative_motion is None
        ):
            return
        raw_item = item.raw_item
        if hasattr(raw_item, "model_dump"):
            raw = raw_item.model_dump(mode="json")
            call_id = raw.get("call_id")
        elif isinstance(raw_item, dict):
            call_id = raw_item.get("call_id")
        else:
            call_id = getattr(raw_item, "call_id", None)
        await self._prepared_relative_motion.discard_call(call_id)
