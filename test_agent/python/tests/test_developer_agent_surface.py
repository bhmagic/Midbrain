from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from physical_agent_test.agent_driver import (
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
    async def preview(self, *, direction: str, distance_m: float):
        return {
            "direction": direction,
            "distance_m": distance_m,
        }

    async def execute(self, **arguments):
        return arguments


class _BasicSafeHomeSkill:
    async def execute(self):
        return {"status": "SAFE_HOME_COMPLETED"}


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
        self.assertTrue(tools["set_provider_residency"].needs_approval)
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
        self.assertTrue(tools["set_provider_residency"].needs_approval)
        self.assertIn("preview_relative_effector_motion", tools)
        self.assertNotIn("retry_last_integrated_motion_target", tools)
        self.assertIn("execute_integrated_motion_preview", tools)
        self.assertIn("execute_basic_safe_home", tools)
        self.assertIn(
            "robot_arm.rebot_dm",
            tools["preview_relative_effector_motion"].description,
        )
        self.assertIn(
            "robot_arm.primary.integrated",
            tools["preview_relative_effector_motion"].description,
        )
        self.assertFalse(
            tools["preview_relative_effector_motion"].needs_approval
        )
        self.assertTrue(
            tools["execute_integrated_motion_preview"].needs_approval
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
            tool_name="execute_integrated_motion_preview",
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
                "value": "Immediate approved MIT one-shot commit",
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
                            "robot.motion.arm.integrated.pos_vel.one_shot"
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
            "robot.motion.arm.integrated.pos_vel.one_shot",
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
        self.assertIn("did not become HOT and ready", result["agent_instruction"])

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
