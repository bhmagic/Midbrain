from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, replace
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
from .integrated_motion_adapter import IntegratedRelativeMotionAdapter
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
        session: Any | None = None,
        defer_loading: bool = False,
        adapter_timeout_s: float = 60.0,
        max_turns: int = 16,
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
        )
        self.offered_skill_descriptors = [
            descriptor
            for descriptor in descriptors
            if descriptor.tool_name in eligible
        ]

        offered_tools = list(tools)
        if defer_loading:
            offered_tools.append(ToolSearchTool())
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
                    {
                        "providers": providers,
                        "capabilities": capabilities,
                        "skills": [
                            descriptor.as_dict()
                            for descriptor in self.offered_skill_descriptors
                        ],
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
                return json.dumps(result, ensure_ascii=False, default=str)

            offered_tools.extend(
                [
                    FunctionTool(
                        name="inspect_midbrain_runtime",
                        description=(
                            "Inspect configured Midbrain Providers, current "
                            "capability availability, and the finite Skills "
                            "offered to this developer agent. This operation "
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
                        name="set_provider_residency",
                        description=(
                            "Request one configured Provider lifecycle "
                            "transition through Manager. Use when the "
                            "user explicitly requests a lifecycle change "
                            "or when a Provider is a necessary cold dependency "
                            "of the requested task. Explain what hardware or "
                            "service may be initialized and use the exact "
                            "Provider ID. Every call requires human approval."
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
                        needs_approval=True,
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
                    distance_m=arguments.get("distance_m"),
                    original_request_distance_m=arguments.get(
                        "original_request_distance_m"
                    ),
                    target_position_m=arguments.get("target_position_m"),
                )
                return json.dumps(result, ensure_ascii=False, default=str)

            offered_tools.extend(
                [
                    FunctionTool(
                        name="execute_integrated_motion_preview",
                        description=(
                            "Execute one exact, fresh Integrated Controller "
                            "relative-motion preview. Copy all arguments "
                            "exactly from preview_relative_effector_motion. "
                            "Human approval immediately sends the existing "
                            "one-shot commit trigger, requests physical arm "
                            "motion, and waits for the controller's bounded "
                            "completion result."
                        ),
                        params_json_schema={
                            "type": "object",
                            "properties": {
                                "preview_id": {"type": "string"},
                                "motion_intent": {
                                    "type": "string",
                                    "enum": ["NEW_RELATIVE_MOVE"],
                                },
                                "direction": {
                                    "type": "string",
                                    "enum": [
                                        "UP",
                                        "DOWN",
                                        "POSITIVE_X",
                                        "NEGATIVE_X",
                                        "POSITIVE_Z",
                                        "NEGATIVE_Z",
                                    ],
                                },
                                "distance_m": {
                                    "type": "number",
                                    "minimum": 0.0,
                                    "maximum": 0.2,
                                },
                                "original_request_distance_m": {
                                    "type": "number",
                                    "minimum": 0.001,
                                    "maximum": 0.2,
                                },
                                "target_position_m": {
                                    "type": "array",
                                    "items": {"type": "number"},
                                    "minItems": 3,
                                    "maxItems": 3,
                                },
                            },
                            "required": [
                                "preview_id",
                                "motion_intent",
                                "direction",
                                "distance_m",
                                "original_request_distance_m",
                                "target_position_m",
                            ],
                            "additionalProperties": False,
                        },
                        on_invoke_tool=execute_integrated_motion_preview,
                        strict_json_schema=True,
                        needs_approval=True,
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
                "You may propose HOT activation for a cold Provider when it "
                "is necessary to fulfill the developer's current task; the "
                "tool call will pause for human approval. Provider lifecycle "
                "changes require human approval. For a requested relative "
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
                    "inspect the runtime and propose the necessary lifecycle "
                    "transition instead of calling an unrelated Skill. Every "
                    "lifecycle call pauses for human approval. Explain the "
                    "dependency and do not claim activation until the Manager "
                    "tool returns success. After an approved activation, "
                    "continue the original task within the same run."
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
                    "execution of that exact preview. Tell the operator that "
                    "approval immediately sends its one-shot commit trigger. "
                    "A PREVIEW_READY result is incomplete: call its "
                    "required_next_tool immediately with unchanged arguments "
                    "instead of answering the user. "
                    "If preview reports DEPENDENCY_UNAVAILABLE, follow its "
                    "required_next_tool and activation sequence; do not repeat "
                    "the same preview call while the controller is unreachable. "
                    "Do not claim motion from the target-edit engagement "
                    "response; report success only when the execution tool "
                    "returns physical_motion_completed=true. Otherwise report "
                    "the controller's completion outcome as an unsuccessful "
                    "or unconfirmed move. Treat every relative motion request "
                    "as a new displacement from the current measured pose, "
                    "including repeated requests."
                )
        if basic_safe_home_skill is not None:
            instructions += (
                " For an explicit safe-home request, use "
                "execute_basic_safe_home after ensuring the Basic Provider "
                "is running. Safe-home preempts active arm control and always "
                "requires approval. Do not substitute gravity float, Provider "
                "stop, or healthy status for homing. Report completion only "
                "when physical_motion_completed=true."
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
    ) -> InteractiveAgentResult:
        result = await self._run(
            input_value,
            model_override=model_override,
            reasoning_effort=reasoning_effort,
            vlm_model_override=vlm_model_override,
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
            title = f"Move the arm {direction} by {distance_text}?"
            summary = (
                "The Integrated Controller has already produced a valid "
                "nonphysical IK preview. Approval requests execution of "
                "that exact staged preview."
            )
            warning = (
                "This is physical arm motion. Keep clear of the arm and be "
                "ready to use the emergency stop. Approval immediately sends "
                "the one-shot commit; no separate controller-button press is "
                "required."
            )
            confirm_label = "Approve arm motion"
            details = [
                {"label": "Direction", "value": direction},
                {"label": "Distance", "value": distance_text},
                {"label": "Intent", "value": motion_intent},
                {"label": "Target XYZ", "value": target_text},
                {
                    "label": "Trigger",
                    "value": "Immediate approved one-shot commit",
                },
                {
                    "label": "Preview",
                    "value": str(arguments.get("preview_id") or "unknown"),
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
