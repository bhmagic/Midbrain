from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from physical_agent_test.agent_driver import (
    AgentSessionAuthorization,
    PrototypeAgentDriver,
    build_midbrain_runtime_snapshot,
    build_turn_safe_session_input,
)


class _PointingSkill:
    async def run(self, question: str) -> str:
        return question


class _Manager:
    async def providers(self):
        return []

    async def capabilities(self):
        return []

    async def set_residency(self, provider_id: str, action: str):
        return {"provider_id": provider_id, "action": action}


class _IntegratedMotionSkill:
    def __init__(self) -> None:
        self.preview_count = 0
        self.world_point_preview_arguments: list[dict] = []
        self.executed_preview_ids: list[str] = []
        self.next_preview_result: dict | None = None

    async def preview(self, **arguments):
        if self.next_preview_result is not None:
            result = self.next_preview_result
            self.next_preview_result = None
            return result
        self.preview_count += 1
        preview_id = f"preview-{self.preview_count}"
        return {
            "status": "PREVIEW_READY",
            "workflow_complete": False,
            "physical_motion_authorized": False,
            "direction": arguments.get("direction"),
            "distance_m": arguments.get("distance_m"),
            "preview_id": preview_id,
            "required_next_tool": {
                "name": "HOST_INTERNAL_SIGNED_PATH_COMMIT",
                "arguments": {"preview_id": preview_id},
            },
        }

    async def preview_world_point(self, **arguments):
        self.world_point_preview_arguments.append(dict(arguments))
        return await self.preview(direction="WORLD_POINT", **arguments)

    async def execute(self, **arguments):
        return arguments

    async def execute_preview(self, *, preview_id: str):
        self.executed_preview_ids.append(preview_id)
        return {
            "status": "MOTION_COMPLETED",
            "workflow_complete": True,
            "physical_motion_completed": True,
            "preview_id": preview_id,
        }

    async def pending_execution_authorization_arguments(self, preview_id):
        return {
            "preview_id": preview_id,
            "motion_intent": "NEW_RELATIVE_MOVE",
            "direction": "UP",
            "distance_m": 0.2,
            "requested_speed_m_s": 0.2,
            "planned_nominal_speed_m_s": 0.2,
            "planned_duration_s": 1.0,
            "target_position_m": [0.1, 0.2, 0.5],
            "orientation_policy": "POSITION_ONLY",
        }


class _BasicSafeHomeSkill:
    async def execute(self):
        return {"status": "SAFE_HOME_COMPLETED"}


class _FabricWorldPointComposer:
    def __init__(self) -> None:
        self.arguments: list[dict] = []

    async def run(self, **arguments):
        self.arguments.append(dict(arguments))
        return {
            "status": "WORLD_POINT_READY",
            "target_position_world_m": [0.4, -0.2, 0.3],
            "target_world_frame_id": "local_vio/epoch-1",
            "target_session_epoch": "epoch-1",
            "physical_motion_authorized": False,
            "physical_motion_submitted": False,
        }


class _FabricSpatialTranslator:
    async def translate_direction(self, **arguments):
        return {
            "status": "WORLD_DIRECTION_READY",
            "direction_world": arguments["direction"],
        }

    async def translate_pose(self, **arguments):
        return {
            "status": "WORLD_POSE_READY",
            "target_position_world_m": arguments["position_m"],
            "target_orientation_world_xyzw": arguments[
                "orientation_xyzw"
            ],
        }

    async def offset_world_point(self, **arguments):
        return {
            "status": "WORLD_POINT_OFFSET_READY",
            "workflow_complete": True,
            "physical_motion_authorized": False,
            "physical_motion_submitted": False,
            "target_position_world_m": arguments["source_position_world_m"],
            "target_world_frame_id": arguments["source_world_frame_id"],
            "target_session_epoch": arguments["source_session_epoch"],
        }


async def _reinitialize_space(reason: str):
    return {"status": "initialized", "reason": reason}


class DeveloperAgentSurfaceTests(unittest.TestCase):
    def test_session_history_limit_keeps_reasoning_call_turn_intact(
        self,
    ) -> None:
        history = [
            {"role": "user", "content": "move the arm"},
            {"type": "reasoning", "id": "rs_required"},
            {
                "type": "function_call",
                "id": "fc_required",
                "call_id": "call_required",
                "name": "inspect_midbrain_runtime",
                "arguments": "{}",
            },
            {
                "type": "function_call_output",
                "call_id": "call_required",
                "output": "{}",
            },
            *[
                {
                    "type": "message",
                    "role": "assistant",
                    "content": f"item-{index}",
                }
                for index in range(30)
            ],
        ]

        combined = build_turn_safe_session_input(
            history,
            [{"role": "user", "content": "establish coordinates"}],
            limit=32,
        )

        self.assertEqual(combined[0]["role"], "user")
        self.assertEqual(combined[1]["id"], "rs_required")
        self.assertEqual(combined[2]["id"], "fc_required")
        self.assertEqual(combined[-1]["content"], "establish coordinates")

    def test_runtime_snapshot_keeps_complete_manager_evidence(
        self,
    ) -> None:
        snapshot = build_midbrain_runtime_snapshot(
            [
                {
                    "config": {
                        "id": "robot_arm.primary.integrated",
                        "display_name": "Integrated",
                        "command": "large command",
                        "env": {
                            "CONTROLLER_MODE": "POSE_6DOF",
                            "SECRET": "must not enter model context",
                        },
                    },
                    "process_state": "running",
                    "report": {
                        "residency": "RECOVERY_REQUIRED",
                        "health": "DEGRADED",
                        "ready": False,
                        "details": {
                            "fault_reason": "Basic lease lost",
                            "ik_mode": "POSE_6DOF",
                            "observed_at_us": 123,
                            "model_view": {"large": ["telemetry"] * 100},
                        },
                        "last_seen": "2026-07-31T12:00:00Z",
                    },
                }
            ],
            [
                {
                    "capability": "robot.motion.arm.integrated.pose_6dof",
                    "provider_id": "robot_arm.primary.integrated",
                    "available": False,
                    "last_seen": "2026-07-31T12:00:01Z",
                }
            ],
            eligible_skill_tools=["calibrate_stationary_workcell"],
        )

        provider = snapshot["providers"][0]
        self.assertEqual(
            provider["report"]["details"]["fault_reason"],
            "Basic lease lost",
        )
        self.assertEqual(
            provider["report"]["details"]["ik_mode"],
            "POSE_6DOF",
        )
        self.assertEqual(
            provider["report"]["details"]["observed_at_us"],
            123,
        )
        self.assertIn("model_view", provider["report"]["details"])
        self.assertEqual(
            provider["report"]["last_seen"],
            "2026-07-31T12:00:00Z",
        )
        self.assertEqual(provider["config"]["command"], "large command")
        self.assertEqual(
            provider["config"]["env"]["CONTROLLER_MODE"],
            "POSE_6DOF",
        )
        self.assertEqual(
            provider["config"]["env"]["SECRET"],
            "[REDACTED]",
        )
        self.assertNotIn("must not enter model context", str(snapshot))
        self.assertEqual(
            snapshot["capabilities"][0]["last_seen"],
            "2026-07-31T12:00:01Z",
        )
        self.assertEqual(
            snapshot["eligible_skill_tools"],
            ["calibrate_stationary_workcell"],
        )

    def test_developer_driver_adds_bounded_provider_tools(self) -> None:
        root = Path(__file__).resolve().parents[3]
        driver = PrototypeAgentDriver(
            _PointingSkill(),
            "gpt-5.6-terra",
            workspace_root=root,
            eligible_tool_names={"identify_pointed_object"},
            manager=_Manager(),
            developer_mode=True,
        )
        tools = {tool.name: tool for tool in driver.agent.tools}

        self.assertEqual(driver.max_turns, 16)
        self.assertIn("inspect_midbrain_runtime", tools)
        self.assertIn("set_provider_residency", tools)
        self.assertFalse(tools["inspect_midbrain_runtime"].needs_approval)
        self.assertTrue(callable(tools["set_provider_residency"].needs_approval))
        self.assertIn(
            "Never answer by asking for conversational permission",
            driver.agent.instructions,
        )
        self.assertIsNone(driver.run_config.session_input_callback)

    def test_regular_driver_exposes_confirmed_provider_lifecycle(self) -> None:
        root = Path(__file__).resolve().parents[3]
        driver = PrototypeAgentDriver(
            _PointingSkill(),
            "gpt-5.6-terra",
            workspace_root=root,
            eligible_tool_names={"identify_pointed_object"},
            manager=_Manager(),
            provider_lifecycle_control=True,
            integrated_motion_skill=_IntegratedMotionSkill(),
            basic_safe_home_skill=_BasicSafeHomeSkill(),
        )
        tools = {tool.name: tool for tool in driver.agent.tools}

        self.assertIn("inspect_midbrain_runtime", tools)
        self.assertIn("set_provider_residency", tools)
        self.assertTrue(callable(tools["set_provider_residency"].needs_approval))
        self.assertIn("perform_relative_effector_motion", tools)
        self.assertIn("move_effector_to_world_point", tools)
        self.assertNotIn("retry_last_integrated_motion_target", tools)
        self.assertNotIn("preview_relative_effector_motion", tools)
        self.assertNotIn("execute_integrated_motion_preview", tools)
        self.assertIn("execute_basic_safe_home", tools)
        self.assertNotIn(
            "robot_arm.rebot_dm",
            tools["perform_relative_effector_motion"].description,
        )
        self.assertNotIn(
            "robot_arm.primary.integrated",
            tools["perform_relative_effector_motion"].description,
        )
        self.assertFalse(
            tools["perform_relative_effector_motion"].needs_approval
        )
        self.assertFalse(tools["move_effector_to_world_point"].needs_approval)
        self.assertEqual(
            tools["move_effector_to_world_point"].params_json_schema[
                "required"
            ],
            [
                "target_position_world_m",
                "target_world_frame_id",
                "target_session_epoch",
                "requested_speed_m_s",
                "execution_backend",
            ],
        )
        self.assertIn(
            "translation_vector_m",
            tools["perform_relative_effector_motion"].params_json_schema[
                "properties"
            ],
        )
        self.assertTrue(tools["execute_basic_safe_home"].needs_approval)
        self.assertIn(
            "instead of answering with a permission request",
            driver.agent.instructions,
        )
        self.assertIn(
            "call set_provider_residency immediately",
            driver.agent.instructions,
        )
        self.assertIn(
            "Manager owns selection and transitive activation",
            driver.agent.instructions,
        )
        self.assertIn(
            "call perform_relative_effector_motion directly",
            driver.agent.instructions,
        )
        self.assertIn(
            "call move_effector_to_world_point directly",
            driver.agent.instructions,
        )
        self.assertNotIn(
            "activate robot_arm.rebot_dm to HOT first",
            driver.agent.instructions,
        )

    def test_fabric_world_point_composer_is_a_separate_read_only_tool(
        self,
    ) -> None:
        root = Path(__file__).resolve().parents[3]
        composer = _FabricWorldPointComposer()
        driver = PrototypeAgentDriver(
            _PointingSkill(),
            "gpt-5.6-terra",
            workspace_root=root,
            eligible_tool_names={"identify_pointed_object"},
            fabric_world_point_composer=composer,
        )
        tools = {tool.name: tool for tool in driver.agent.tools}

        self.assertIn("derive_fabric_world_point", tools)
        tool = tools["derive_fabric_world_point"]
        self.assertFalse(tool.needs_approval)
        self.assertEqual(
            tool.params_json_schema["required"],
            [
                "object_id",
                "corner_name",
                "offset_vector",
                "offset_unit",
                "offset_reference",
                "expected_scene_revision",
            ],
        )
        self.assertNotIn(
            "requested_speed_m_s",
            tool.params_json_schema["properties"],
        )

    def test_fabric_direction_and_pose_translators_are_typed_read_only_tools(
        self,
    ) -> None:
        root = Path(__file__).resolve().parents[3]
        driver = PrototypeAgentDriver(
            _PointingSkill(),
            "gpt-5.6-terra",
            workspace_root=root,
            eligible_tool_names={"identify_pointed_object"},
            fabric_spatial_translator=_FabricSpatialTranslator(),
        )
        tools = {tool.name: tool for tool in driver.agent.tools}

        direction = tools["translate_fabric_direction_to_world"]
        self.assertFalse(direction.needs_approval)
        self.assertEqual(
            direction.params_json_schema["required"],
            [
                "direction",
                "source_reference",
                "source_frame_id",
                "source_observed_at_us",
                "source_session_epoch",
            ],
        )
        self.assertEqual(
            direction.params_json_schema["properties"]["source_reference"][
                "enum"
            ],
            [
                "ACTIVE_WORLD",
                "ARM_BASE",
                "CONTROLLED_EFFECTOR_FRAME",
            ],
        )

        pose = tools["translate_fabric_pose_to_world"]
        self.assertFalse(pose.needs_approval)
        self.assertIn("position_m", pose.params_json_schema["required"])
        self.assertIn("orientation_xyzw", pose.params_json_schema["required"])
        self.assertNotIn(
            "requested_speed_m_s",
            pose.params_json_schema["properties"],
        )
        offset = tools["offset_world_point"]
        self.assertFalse(offset.needs_approval)
        self.assertEqual(
            offset.params_json_schema["properties"]["offset_reference"]["enum"],
            [
                "ACTIVE_WORLD",
                "ARM_BASE",
                "CONTROLLED_EFFECTOR_FRAME",
            ],
        )
        self.assertIn(
            "source_position_world_m",
            offset.params_json_schema["required"],
        )
        self.assertIn(
            "never authorizes or submits motion",
            driver.agent.instructions,
        )
        self.assertIn("offset_world_point", driver.agent.instructions)

    def test_provider_approval_is_human_readable(self) -> None:
        item = SimpleNamespace(
            tool_name="set_provider_residency",
            tool_namespace=None,
            raw_item={
                "arguments": (
                    '{"provider_id":"camera.femto_bolt","action":"hot"}'
                )
            },
        )

        approval = PrototypeAgentDriver._approval_description(item)

        self.assertEqual(
            approval["title"],
            "Start and activate camera.femto_bolt?",
        )
        self.assertIn("initialize attached hardware", approval["warning"])
        self.assertEqual(
            approval["details"],
            [
                {"label": "Provider", "value": "camera.femto_bolt"},
                {"label": "Requested state", "value": "HOT"},
            ],
        )
    def test_motion_approval_explains_immediate_one_shot_commit(self) -> None:
        item = SimpleNamespace(
            tool_name="perform_relative_effector_motion",
            tool_namespace=None,
            raw_item={
                "arguments": (
                    '{"preview_id":"preview-1","direction":"UP",'
                    '"motion_intent":"NEW_RELATIVE_MOVE",'
                    '"distance_m":0.2,"original_request_distance_m":0.2,'
                    '"requested_speed_m_s":0.2,'
                    '"planned_duration_s":1.2,'
                    '"target_position_m":[0.1,0.4,0.3]}'
                )
            },
        )

        approval = PrototypeAgentDriver._approval_description(item)

        self.assertEqual(approval["title"], "Move the arm UP by 20 cm?")
        self.assertIn(
            "no separate controller-button press",
            approval["warning"],
        )
        self.assertIn(
            {
                "label": "Trigger",
                "value": "Autonomous signed free-space path commit",
            },
            approval["details"],
        )
        self.assertIn(
            {
                "label": "Requested speed",
                "value": "0.2 m/s nominal average",
            },
            approval["details"],
        )
        self.assertIn(
            {"label": "Planned duration", "value": "1.2 s"},
            approval["details"],
        )

    def test_safe_home_approval_is_human_readable(self) -> None:
        item = SimpleNamespace(
            tool_name="execute_basic_safe_home",
            tool_namespace=None,
            raw_item={"arguments": "{}"},
        )

        approval = PrototypeAgentDriver._approval_description(item)

        self.assertEqual(
            approval["title"],
            "Move the arm to configured safe-home?",
        )
        self.assertIn("physical arm motion", approval["warning"])

    def test_space_reinitialization_is_typed_and_approval_gated(self) -> None:
        root = Path(__file__).resolve().parents[3]
        driver = PrototypeAgentDriver(
            _PointingSkill(),
            "gpt-5.6-terra",
            workspace_root=root,
            eligible_tool_names={"reinitialize_space_cognition"},
            space_cognition_reinitializer=_reinitialize_space,
        )
        tools = {tool.name: tool for tool in driver.agent.tools}

        self.assertIn("reinitialize_space_cognition", tools)
        self.assertTrue(tools["reinitialize_space_cognition"].needs_approval)

        item = SimpleNamespace(
            tool_name="reinitialize_space_cognition",
            tool_namespace=None,
            raw_item={
                "arguments": '{"reason":"recover from accumulated VIO drift"}'
            },
        )
        approval = PrototypeAgentDriver._approval_description(item)

        self.assertEqual(
            approval["title"],
            "Establish a new Midbrain spatial origin?",
        )
        self.assertIn("revokes active", approval["warning"])


class DeveloperAgentLifecycleResultTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _relative_request() -> dict[str, object]:
        return {
            "direction": "UP",
            "reference_frame": "WORLD",
            "arm_mount_assumption": "UNKNOWN",
            "camera_level_assumption": "UNKNOWN",
            "fixed_vio_rig_assumption": "UNKNOWN",
            "orientation_policy": "POSITION_ONLY",
            "controlled_frame_yaw_delta_deg": None,
            "distance_m": 0.2,
            "requested_speed_m_s": None,
        }

    async def test_raw_motion_continuation_tools_are_not_exposed(self) -> None:
        root = Path(__file__).resolve().parents[3]
        driver = PrototypeAgentDriver(
            _PointingSkill(),
            "gpt-5.6-terra",
            workspace_root=root,
            eligible_tool_names={"identify_pointed_object"},
            integrated_motion_skill=_IntegratedMotionSkill(),
        )
        names = {item.name for item in driver.agent.tools}
        self.assertNotIn("preview_relative_effector_motion", names)
        self.assertNotIn("execute_integrated_motion_preview", names)

    async def test_prepared_motion_auto_authorizes_and_executes_same_call(
        self,
    ) -> None:
        from agents.run_context import RunContextWrapper

        root = Path(__file__).resolve().parents[3]
        skill = _IntegratedMotionSkill()
        driver = PrototypeAgentDriver(
            _PointingSkill(),
            "gpt-5.6-terra",
            workspace_root=root,
            eligible_tool_names={"identify_pointed_object"},
            integrated_motion_skill=skill,
        )
        tool = next(
            item
            for item in driver.agent.tools
            if item.name == "perform_relative_effector_motion"
        )
        arguments = self._relative_request()

        result = json.loads(
            await tool.on_invoke_tool(
                SimpleNamespace(tool_call_id="call-prepared"),
                json.dumps(arguments),
            )
        )

        self.assertFalse(tool.needs_approval)
        self.assertEqual(result["status"], "MOTION_COMPLETED")
        self.assertEqual(skill.preview_count, 1)
        self.assertEqual(skill.executed_preview_ids, ["preview-1"])

    async def test_prepared_motion_executes_without_session_approval(
        self,
    ) -> None:
        from agents.run_context import RunContextWrapper

        root = Path(__file__).resolve().parents[3]
        skill = _IntegratedMotionSkill()
        driver = PrototypeAgentDriver(
            _PointingSkill(),
            "gpt-5.6-terra",
            workspace_root=root,
            eligible_tool_names={"identify_pointed_object"},
            integrated_motion_skill=skill,
        )
        tool = next(
            item
            for item in driver.agent.tools
            if item.name == "perform_relative_effector_motion"
        )
        arguments = self._relative_request()
        result = json.loads(
            await tool.on_invoke_tool(
                SimpleNamespace(tool_call_id="call-autonomous"),
                json.dumps(arguments),
            )
        )

        self.assertFalse(tool.needs_approval)
        self.assertEqual(result["status"], "MOTION_COMPLETED")
        self.assertEqual(skill.executed_preview_ids, ["preview-1"])

    async def test_world_point_motion_is_call_scoped_and_preserves_source_identity(
        self,
    ) -> None:
        root = Path(__file__).resolve().parents[3]
        skill = _IntegratedMotionSkill()
        driver = PrototypeAgentDriver(
            _PointingSkill(),
            "gpt-5.6-terra",
            workspace_root=root,
            eligible_tool_names={"identify_pointed_object"},
            integrated_motion_skill=skill,
        )
        tool = next(
            item
            for item in driver.agent.tools
            if item.name == "move_effector_to_world_point"
        )
        arguments = {
            "target_position_world_m": [0.5, 0.25, -0.18],
            "target_world_frame_id": "local_vio/epoch-7",
            "target_session_epoch": "epoch-7",
            "requested_speed_m_s": None,
            "execution_backend": "IMPEDANCE",
        }

        result = json.loads(
            await tool.on_invoke_tool(
                SimpleNamespace(tool_call_id="call-world-point"),
                json.dumps(arguments),
            )
        )

        self.assertFalse(tool.needs_approval)
        self.assertEqual(result["status"], "MOTION_COMPLETED")
        self.assertEqual(skill.executed_preview_ids, ["preview-1"])
        self.assertEqual(skill.world_point_preview_arguments, [arguments])

    async def test_prepared_motion_does_not_chain_dependency_continuation(
        self,
    ) -> None:
        from agents.run_context import RunContextWrapper

        root = Path(__file__).resolve().parents[3]
        skill = _IntegratedMotionSkill()
        skill.next_preview_result = {
            "status": "DEPENDENCY_UNAVAILABLE",
            "workflow_complete": False,
            "physical_motion_authorized": False,
            "required_next_tool": {
                "name": "set_provider_residency",
                "arguments": {
                    "provider_id": "robot_arm.primary.integrated",
                    "action": "hot",
                    "required_capability": (
                        "robot_arm.motion.free_space.preview_commit.v1"
                    ),
                },
            },
        }
        driver = PrototypeAgentDriver(
            _PointingSkill(),
            "gpt-5.6-terra",
            workspace_root=root,
            eligible_tool_names={"identify_pointed_object"},
            integrated_motion_skill=skill,
        )
        tool = next(
            item
            for item in driver.agent.tools
            if item.name == "perform_relative_effector_motion"
        )
        arguments = self._relative_request()

        result = json.loads(
            await tool.on_invoke_tool(
                SimpleNamespace(tool_call_id="call-dependency"),
                json.dumps(arguments),
            )
        )

        self.assertFalse(tool.needs_approval)
        self.assertEqual(result["status"], "DEPENDENCY_UNAVAILABLE")
        self.assertEqual(skill.executed_preview_ids, [])

    async def test_unadvertised_model_capability_does_not_false_timeout(
        self,
    ) -> None:
        class _ReadyManager(_Manager):
            async def providers(self):
                return [
                    {
                        "config": {"id": "robot_arm.primary.integrated"},
                        "process_state": "running",
                        "report": {
                            "provider_id": "robot_arm.primary.integrated",
                            "residency": "HOT",
                            "health": "HEALTHY",
                            "ready": True,
                            "expired": False,
                        },
                    }
                ]

            async def capabilities(self):
                return [
                    {
                        "capability": (
                            "robot_arm.motion.free_space.preview_commit.v1"
                        ),
                        "provider_id": "robot_arm.primary.integrated",
                        "available": True,
                        "ready": True,
                        "expired": False,
                    }
                ]

        root = Path(__file__).resolve().parents[3]
        driver = PrototypeAgentDriver(
            _PointingSkill(),
            "gpt-test",
            workspace_root=root,
            eligible_tool_names={"identify_pointed_object"},
            manager=_ReadyManager(),
            developer_mode=True,
            provider_hot_readiness_timeout_s=0.1,
            provider_hot_readiness_poll_interval_s=0.001,
        )
        tool = {
            candidate.name: candidate for candidate in driver.agent.tools
        }["set_provider_residency"]

        result = json.loads(
            await tool.on_invoke_tool(
                None,
                json.dumps(
                    {
                        "provider_id": "robot_arm.primary.integrated",
                        "action": "hot",
                        "required_capability": "robot.motion.arm.integrated",
                    }
                ),
            )
        )

        self.assertTrue(result["lifecycle_request_complete"])
        self.assertEqual(result["readiness"]["status"], "READY")
        self.assertFalse(result["readiness"]["capability_advertised"])
        self.assertIsNone(result["readiness"]["capability_ready"])
        self.assertIn(
            "robot_arm.motion.free_space.preview_commit.v1",
            result["readiness"]["advertised_capabilities"],
        )
        self.assertIn("advisory model guess", result["agent_instruction"])

    async def test_start_dependency_waits_for_hot_capability_then_continues(
        self,
    ) -> None:
        class _ConfiguredManager(_Manager):
            def __init__(self) -> None:
                self.hot_requested = False
                self.readiness_polls = 0

            async def providers(self):
                if self.hot_requested:
                    self.readiness_polls += 1
                ready = self.readiness_polls >= 2
                return [
                    {
                        "config": {"id": "camera.femto_bolt"},
                        "process_state": "running",
                        "report": {
                            "provider_id": "camera.femto_bolt",
                            "residency": "HOT" if ready else "WARM",
                            "health": "HEALTHY",
                            "ready": ready,
                            "expired": False,
                        },
                    }
                ]

            async def capabilities(self):
                ready = self.readiness_polls >= 2
                return [
                    {
                        "capability": "camera.rgb",
                        "provider_id": "camera.femto_bolt",
                        "available": ready,
                        "ready": ready,
                        "expired": False,
                    }
                ]

            async def set_residency(self, provider_id: str, action: str):
                self.hot_requested = True
                return await super().set_residency(provider_id, action)

        root = Path(__file__).resolve().parents[3]
        manager = _ConfiguredManager()
        driver = PrototypeAgentDriver(
            _PointingSkill(),
            "gpt-test",
            workspace_root=root,
            eligible_tool_names={"identify_pointed_object"},
            manager=manager,
            developer_mode=True,
            provider_hot_readiness_timeout_s=0.1,
            provider_hot_readiness_poll_interval_s=0.001,
        )
        tool = {
            candidate.name: candidate for candidate in driver.agent.tools
        }["set_provider_residency"]

        raw = await tool.on_invoke_tool(
            None,
            json.dumps(
                {
                    "provider_id": "camera.femto_bolt",
                    "action": "start",
                    "required_capability": "camera.rgb",
                }
            ),
        )
        result = json.loads(raw)

        self.assertTrue(result["lifecycle_request_accepted"])
        self.assertTrue(result["lifecycle_request_complete"])
        self.assertEqual(result["requested_action"], "START")
        self.assertEqual(result["required_capability"], "camera.rgb")
        self.assertEqual(result["readiness"]["status"], "READY")
        self.assertTrue(result["readiness"]["capability_ready"])
        self.assertIsNone(result["required_next_tool"])
        self.assertGreaterEqual(manager.readiness_polls, 2)
        self.assertIn("Do not request", result["agent_instruction"])
        self.assertIn("invoke that original Skill", result["agent_instruction"])
        self.assertEqual(
            tool.params_json_schema["required"],
            ["provider_id", "action", "required_capability"],
        )

    async def test_hot_lifecycle_reports_bounded_readiness_timeout(self) -> None:
        class _NeverReadyManager(_Manager):
            async def providers(self):
                return [
                    {
                        "config": {"id": "camera.femto_bolt"},
                        "process_state": "running",
                        "report": {
                            "provider_id": "camera.femto_bolt",
                            "residency": "HOT",
                            "health": "DEGRADED",
                            "ready": False,
                            "expired": False,
                            "details": {
                                "last_error": "semantic coverage is unavailable",
                                "diagnostics": {
                                    "status": "WAITING_FOR_INITIAL_VLM_ANNOTATION"
                                },
                            },
                        },
                    }
                ]

        root = Path(__file__).resolve().parents[3]
        driver = PrototypeAgentDriver(
            _PointingSkill(),
            "gpt-test",
            workspace_root=root,
            eligible_tool_names={"identify_pointed_object"},
            manager=_NeverReadyManager(),
            developer_mode=True,
            provider_hot_readiness_timeout_s=0.005,
            provider_hot_readiness_poll_interval_s=0.001,
        )
        tool = {
            candidate.name: candidate for candidate in driver.agent.tools
        }["set_provider_residency"]

        result = json.loads(
            await tool.on_invoke_tool(
                None,
                json.dumps(
                    {
                        "provider_id": "camera.femto_bolt",
                        "action": "hot",
                        "required_capability": None,
                    }
                ),
            )
        )

        self.assertTrue(result["lifecycle_request_accepted"])
        self.assertFalse(result["lifecycle_request_complete"])
        self.assertEqual(result["readiness"]["status"], "TIMED_OUT")
        self.assertFalse(result["readiness"]["provider_ready"])
        self.assertEqual(
            result["readiness"]["provider"]["last_error"],
            "semantic coverage is unavailable",
        )
        self.assertEqual(
            result["readiness"]["provider"]["diagnostics"]["status"],
            "WAITING_FOR_INITIAL_VLM_ANNOTATION",
        )
        self.assertIn("did not become HOT and ready", result["agent_instruction"])

    async def test_hot_timeout_reports_manager_resolved_dependency_diagnostics(
        self,
    ) -> None:
        class _SceneManager(_Manager):
            async def providers(self):
                return [
                    {
                        "config": {"id": "world_model.arm_scene_compiler"},
                        "process_state": "running",
                        "report": {
                            "provider_id": "world_model.arm_scene_compiler",
                            "residency": "HOT",
                            "health": "DEGRADED",
                            "ready": False,
                            "expired": False,
                            "details": {
                                "last_error": (
                                    "REQUIRED_SEMANTIC_COVERAGE_UNAVAILABLE"
                                )
                            },
                        },
                    },
                    {
                        "config": {"id": "perception.sam2_scene_tracker"},
                        "process_state": "running",
                        "report": {
                            "provider_id": "perception.sam2_scene_tracker",
                            "residency": "HOT",
                            "health": "DEGRADED",
                            "ready": False,
                            "expired": False,
                            "details": {
                                "last_error": "semantic coverage is not ready",
                                "diagnostics": {
                                    "status": "WAITING_FOR_INITIAL_VLM_ANNOTATION"
                                },
                            },
                        },
                    },
                ]

            async def set_residency(self, provider_id: str, action: str):
                return {
                    "provider_id": provider_id,
                    "action": action,
                    "manager_hot_dependencies": [
                        "perception.sam2_scene_tracker"
                    ],
                }

        root = Path(__file__).resolve().parents[3]
        driver = PrototypeAgentDriver(
            _PointingSkill(),
            "gpt-test",
            workspace_root=root,
            eligible_tool_names={"identify_pointed_object"},
            manager=_SceneManager(),
            developer_mode=True,
            provider_hot_readiness_timeout_s=0.001,
            provider_hot_readiness_timeout_overrides_s={
                "world_model.arm_scene_compiler": 0.005
            },
            provider_hot_readiness_poll_interval_s=0.001,
        )
        tool = {
            candidate.name: candidate for candidate in driver.agent.tools
        }["set_provider_residency"]

        result = json.loads(
            await tool.on_invoke_tool(
                None,
                json.dumps(
                    {
                        "provider_id": "world_model.arm_scene_compiler",
                        "action": "hot",
                        "required_capability": "world_model.arm.semantic_scene",
                    }
                ),
            )
        )

        self.assertEqual(result["readiness"]["timeout_s"], 0.005)
        self.assertEqual(
            result["readiness"]["provider"]["last_error"],
            "REQUIRED_SEMANTIC_COVERAGE_UNAVAILABLE",
        )
        self.assertEqual(
            result["readiness"]["dependencies"][0]["provider_id"],
            "perception.sam2_scene_tracker",
        )
        self.assertEqual(
            result["readiness"]["dependencies"][0]["diagnostics"]["status"],
            "WAITING_FOR_INITIAL_VLM_ANNOTATION",
        )

    async def test_hot_readiness_stops_on_structured_external_prerequisite(
        self,
    ) -> None:
        class _BlockedSceneManager(_Manager):
            async def providers(self):
                return [
                    {
                        "config": {"id": "world_model.arm_scene_compiler"},
                        "process_state": "running",
                        "report": {
                            "provider_id": "world_model.arm_scene_compiler",
                            "residency": "HOT",
                            "health": "DEGRADED",
                            "ready": False,
                            "expired": False,
                        },
                    },
                    {
                        "config": {"id": "perception.sam2_scene_tracker"},
                        "process_state": "running",
                        "report": {
                            "provider_id": "perception.sam2_scene_tracker",
                            "residency": "HOT",
                            "health": "DEGRADED",
                            "ready": False,
                            "expired": False,
                            "details": {
                                "diagnostics": {
                                    "status": (
                                        "CAMERA_TO_ARM_TRANSFORM_UNAVAILABLE_"
                                        "2D_TRACKING_ACTIVE"
                                    ),
                                    "blocking_prerequisite": {
                                        "status": "TRANSFORM_UNAVAILABLE",
                                        "requires_external_action": True,
                                        "from_frame": "camera",
                                        "to_frame": "rebot_arm_base",
                                    },
                                }
                            },
                        },
                    },
                ]

            async def set_residency(self, provider_id: str, action: str):
                return {
                    "provider_id": provider_id,
                    "action": action,
                    "manager_hot_dependencies": [
                        "perception.sam2_scene_tracker"
                    ],
                }

        root = Path(__file__).resolve().parents[3]
        driver = PrototypeAgentDriver(
            _PointingSkill(),
            "gpt-test",
            workspace_root=root,
            eligible_tool_names={"identify_pointed_object"},
            manager=_BlockedSceneManager(),
            developer_mode=True,
            provider_hot_readiness_timeout_s=30.0,
            provider_hot_readiness_poll_interval_s=0.001,
        )
        tool = {
            candidate.name: candidate for candidate in driver.agent.tools
        }["set_provider_residency"]

        result = json.loads(
            await tool.on_invoke_tool(
                None,
                json.dumps(
                    {
                        "provider_id": "world_model.arm_scene_compiler",
                        "action": "hot",
                        "required_capability": "world_model.arm.semantic_scene",
                    }
                ),
            )
        )

        self.assertEqual(
            result["readiness"]["status"],
            "BLOCKED_BY_PREREQUISITE",
        )
        self.assertFalse(result["lifecycle_request_complete"])
        self.assertEqual(
            result["readiness"]["blocking_prerequisites"][0]["status"],
            "TRANSFORM_UNAVAILABLE",
        )
        self.assertIn("external prerequisite", result["agent_instruction"])

    async def test_start_dependency_timeout_requires_exact_hot_continuation(
        self,
    ) -> None:
        class _NeverReadyManager(_Manager):
            async def providers(self):
                return [
                    {
                        "config": {"id": "camera.femto_bolt"},
                        "process_state": "running",
                        "report": {
                            "provider_id": "camera.femto_bolt",
                            "residency": "WARM",
                            "health": "HEALTHY",
                            "ready": False,
                            "expired": False,
                        },
                    }
                ]

            async def capabilities(self):
                return []

        root = Path(__file__).resolve().parents[3]
        driver = PrototypeAgentDriver(
            _PointingSkill(),
            "gpt-test",
            workspace_root=root,
            eligible_tool_names={"identify_pointed_object"},
            manager=_NeverReadyManager(),
            developer_mode=True,
            provider_hot_readiness_timeout_s=0.005,
            provider_hot_readiness_poll_interval_s=0.001,
        )
        tool = {
            candidate.name: candidate for candidate in driver.agent.tools
        }["set_provider_residency"]

        result = json.loads(
            await tool.on_invoke_tool(
                None,
                json.dumps(
                    {
                        "provider_id": "camera.femto_bolt",
                        "action": "start",
                        "required_capability": "camera.rgb",
                    }
                ),
            )
        )

        self.assertFalse(result["lifecycle_request_complete"])
        self.assertEqual(
            result["required_next_tool"],
            {
                "name": "set_provider_residency",
                "arguments": {
                    "provider_id": "camera.femto_bolt",
                    "action": "hot",
                    "required_capability": "camera.rgb",
                },
            },
        )


if __name__ == "__main__":
    unittest.main()
