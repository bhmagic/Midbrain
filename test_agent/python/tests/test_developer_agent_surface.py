from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

from physical_agent_test.agent_driver import PrototypeAgentDriver


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


class DeveloperAgentSurfaceTests(unittest.TestCase):
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
                "value": "Immediate approved one-shot commit",
            },
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


if __name__ == "__main__":
    unittest.main()
