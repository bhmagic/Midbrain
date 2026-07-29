from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from agents import (
    Agent,
    ModelSettings,
    RunConfig,
    Runner,
    ToolExecutionConfig,
    ToolSearchTool,
)

from .effector_front_adapter import EffectorFrontSkillAdapter
from .gemini_pointing_skill import (
    PointingIdentificationSkill,
    VisualSceneAnalysisSkill,
)
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
        defer_loading: bool = False,
        adapter_timeout_s: float = 60.0,
    ):
        self.skill = skill
        root = workspace_root or Path(__file__).resolve().parents[3]
        eligible = eligible_tool_names or {"identify_pointed_object"}
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
        narrow_initial = eligible == {"identify_pointed_object"}
        reviewed_execution_only = eligible == {
            "execute_reviewed_observation_motion"
        }
        if reviewed_execution_only:
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
                "This is a deliberately narrow initial skill-discovery evaluation for a "
                "physical-agent platform. "
                if narrow_initial
                else "This is a Phase 4 finite-Skill discovery evaluation for a physical-agent platform. "
            )
            instructions += (
                "Select a skill from its name and description; do "
                "not infer a provider from brand names. For this evaluation, every accepted "
                "request concerns the current camera scene and you must call exactly one "
                "eligible finite skill before answering. The eligible tool allowlist is "
                f"{', '.join(sorted(eligible))}. Pass the user's complete request to it. Treat its "
                "JSON as the complete sensor evidence, explain the result plainly, and mention "
                "uncertainty. Never invent depth, coordinates, motion, contact, or additional "
                "sensor evidence. Never claim that the robot moved or interacted with an "
                "object. Provider selection is advisory Manager work; an explicit provider ID "
                "may appear only as a reported compatibility fallback."
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

    async def run(self, prompt: str) -> str:
        if not os.getenv("OPENAI_API_KEY", "").strip():
            raise RuntimeError("OPENAI_API_KEY is empty in config/api_keys.env")
        report_operation_progress("AGENT_MODEL_SELECTION")
        result = await await_with_progress_heartbeat(
            Runner.run(
                self.agent,
                prompt,
                max_turns=7,
                run_config=self.run_config,
            ),
            stage="AGENT_MODEL_AWAITING_RESPONSE",
        )
        report_operation_progress("AGENT_RUN_COMPLETED")
        output = result.final_output
        if isinstance(output, str):
            return output
        return json.dumps(output, ensure_ascii=False, default=str)
