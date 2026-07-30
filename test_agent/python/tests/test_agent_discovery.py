from __future__ import annotations

import json
import unittest
from pathlib import Path

import pytest

pytest.importorskip("agents")

from physical_agent_test.agent_driver import PrototypeAgentDriver
from physical_agent_test.gemini_pointing_skill import PointingIdentificationSkill
from physical_agent_test.skill_catalog import discover_agent_skills


class _PointingSkill:
    async def run(self, question: str) -> str:
        return question


class _EffectorFrontSkill:
    async def run(self, *, target_frame: str) -> dict[str, object]:
        return {
            "target_frame": target_frame,
            "physical_action_submitted": False,
        }


class _ReviewedExecutionSkill:
    async def run(self, *, decision_id: str) -> dict[str, object]:
        return {
            "decision_id": decision_id,
            "status": "COMPLETED",
            "model_supplied_motion_parameters": False,
        }


class _FailingManager:
    async def bind_capabilities(self, *_args, **_kwargs):
        raise RuntimeError("old Manager has no binding endpoint")


class _BindingManager:
    def __init__(self):
        self.revalidation_count = 0

    async def bind_capabilities(self, *_args, **_kwargs):
        return {
            "binding_id": "binding-1",
            "status": "RESOLVED",
            "validity": "PENDING_VALIDATION",
        }

    async def capability_binding(self, binding_id: str):
        self.revalidation_count += 1
        return {
            "binding_id": binding_id,
            "status": "RESOLVED",
            "validity": "CURRENT",
            "validation_issues": [],
            "selections": [
                {
                    "capability": "camera.rgb",
                    "provider_id": "camera.femto_bolt",
                }
            ],
        }


class _ColdBindingManager:
    base_url = "http://127.0.0.1:7001"

    async def bind_capabilities(self, *_args, **_kwargs):
        return {
            "binding_id": "binding-cold",
            "status": "RESOLVED",
            "validity": "FALLBACK_REQUIRES_ACTIVATION",
            "selections": [
                {
                    "capability": "camera.rgb",
                    "provider_id": "camera.femto_bolt",
                    "requires_activation": True,
                }
            ],
        }

    async def capability_binding(self, _binding_id: str):
        return await self.bind_capabilities()


class AgentDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def test_catalog_exposes_only_discoverable_skills_by_default(self) -> None:
        workspace = Path(__file__).resolve().parents[3]

        descriptors = discover_agent_skills(workspace)

        self.assertEqual(
            [descriptor.tool_name for descriptor in descriptors],
            [
                "analyze_visual_scene",
                "calibrate_stationary_workcell",
                "execute_reviewed_observation_motion",
                "identify_pointed_object",
                "locate_effector_front",
                "preview_relative_effector_motion",
                "register_rgbd_pixel_to_world",
                "register_tool_to_control_frame",
                "reinitialize_space_cognition",
                "verify_rgbd_image_alignment",
            ],
        )
        relative_motion = next(
            descriptor
            for descriptor in descriptors
            if descriptor.tool_name == "preview_relative_effector_motion"
        )
        self.assertEqual(
            [
                item["provider_id"]
                for item in relative_motion.route_policy[
                    "provider_activation_sequence"
                ]
            ],
            [
                "robot_arm.rebot_dm",
                "robot_arm.primary.integrated",
            ],
        )
        self.assertEqual(
            descriptors[1].required_capabilities[0],
            "camera.rgb",
        )
        pointing = next(
            descriptor
            for descriptor in descriptors
            if descriptor.tool_name == "identify_pointed_object"
        )
        self.assertEqual(
            pointing.execution_adapter_id,
            "test_agent.identify_pointed_object.v1",
        )
        self.assertEqual(pointing.input_schema["required"], ["question"])
        registration = next(
            descriptor
            for descriptor in descriptors
            if descriptor.tool_name == "register_rgbd_pixel_to_world"
        )
        self.assertEqual(
            registration.route_policy["preference_order"],
            [
                "camera.rgbd.route.generic_shared_memory",
                "camera.rgbd.route.direct_shared_memory",
            ],
        )

    def test_catalog_keeps_disabled_local_skill_visible_for_debugging(self) -> None:
        workspace = Path(__file__).resolve().parents[3]

        descriptors = discover_agent_skills(workspace, include_disabled=True)
        by_name = {descriptor.tool_name: descriptor for descriptor in descriptors}

        cutting = by_name.get("vegetable_cutting_legacy_local")
        if cutting is not None:
            self.assertFalse(cutting.discoverable)
            self.assertEqual(cutting.safety_class, "MANUAL_ONLY")
            self.assertTrue(cutting.disabled_reason)
        observation = by_name["observe_pointed_object_from_pose"]
        self.assertFalse(observation.discoverable)
        self.assertIn("structured pointing-pixel", observation.disabled_reason)
        foundation = by_name["localize_known_cad_object"]
        self.assertFalse(foundation.discoverable)
        self.assertEqual(
            foundation.execution_adapter_kind,
            "MANUAL_LOCAL_ONLY",
        )

    def test_rgbd_skills_bind_geometry_without_making_generic_route_mandatory(
        self,
    ) -> None:
        workspace = Path(__file__).resolve().parents[3]
        for package in (
            "locate-effector-front",
            "spatial_registration_rgbd",
            "register_tool_to_control_frame",
        ):
            manifest = json.loads(
                (workspace / "skills" / package / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertNotIn(
                "camera.rgbd.route.generic_shared_memory",
                manifest["required_capabilities"],
            )
            self.assertEqual(
                manifest["route_policy"]["preference_order"],
                [
                    "camera.rgbd.route.generic_shared_memory",
                    "camera.rgbd.route.direct_shared_memory",
                ],
            )
            self.assertEqual(manifest["route_policy"]["required_route_count"], 1)

    async def test_effector_front_manifest_invokes_read_only_adapter(
        self,
    ) -> None:
        driver = PrototypeAgentDriver(
            _PointingSkill(),
            "gpt-test",
            tool_choice="required",
            eligible_tool_names={"locate_effector_front"},
            effector_front_skill=_EffectorFrontSkill(),  # type: ignore[arg-type]
        )

        self.assertEqual(
            driver.agent.tools[0].name,
            "locate_effector_front",
        )
        result = await driver.agent.tools[0].on_invoke_tool(
            None,  # type: ignore[arg-type]
            '{"target_frame":"stationary_world"}',
        )
        parsed = json.loads(result)
        self.assertEqual(parsed["target_frame"], "stationary_world")
        self.assertFalse(parsed["physical_action_submitted"])

    async def test_reviewed_execution_manifest_exposes_only_decision_id(
        self,
    ) -> None:
        driver = PrototypeAgentDriver(
            _PointingSkill(),
            "gpt-test",
            tool_choice="required",
            eligible_tool_names={"execute_reviewed_observation_motion"},
            reviewed_observation_execution_skill=(
                _ReviewedExecutionSkill()  # type: ignore[arg-type]
            ),
            defer_loading=False,
        )

        self.assertEqual(len(driver.agent.tools), 1)
        tool = driver.agent.tools[0]
        self.assertEqual(tool.name, "execute_reviewed_observation_motion")
        self.assertFalse(tool.needs_approval)
        self.assertEqual(
            tool.params_json_schema["required"],
            ["decision_id"],
        )
        result = await tool.on_invoke_tool(
            None,  # type: ignore[arg-type]
            '{"decision_id":"decision-1"}',
        )
        parsed = json.loads(result)
        self.assertEqual(parsed["status"], "COMPLETED")
        self.assertFalse(parsed["model_supplied_motion_parameters"])
        self.assertIn(
            "decision-specific physical execution boundary",
            str(driver.agent.instructions),
        )

    def test_initial_agent_policy_requires_skill_selection(self) -> None:
        driver = PrototypeAgentDriver(
            _PointingSkill(),
            "gpt-test",
            tool_choice="required",
        )

        self.assertEqual(driver.agent.model_settings.tool_choice, "required")
        self.assertFalse(driver.agent.model_settings.parallel_tool_calls)
        self.assertIn(
            "deliberately narrow initial agent surface",
            str(driver.agent.instructions),
        )
        self.assertEqual(driver.agent.tools[0].name, "identify_pointed_object")
        self.assertIn("read-only finite Skill", driver.agent.tools[0].description)
        self.assertEqual(
            driver.agent.tools[0].params_json_schema["required"],
            ["question"],
        )

    async def test_manifest_tool_invokes_registered_adapter(self) -> None:
        driver = PrototypeAgentDriver(
            _PointingSkill(),
            "gpt-test",
            tool_choice="required",
        )

        result = await driver.agent.tools[0].on_invoke_tool(
            None,  # type: ignore[arg-type]
            '{"question":"Which object?"}',
        )

        self.assertEqual(result, "Which object?")

    async def test_camera_binding_falls_back_when_advisory_manager_is_unavailable(
        self,
    ) -> None:
        skill = PointingIdentificationSkill(
            capture=None,  # type: ignore[arg-type]
            model="test-model",
            manager=_FailingManager(),  # type: ignore[arg-type]
            fallback_camera_provider_id="camera.femto_bolt",
        )

        binding = await skill._bind_camera("skill-1")

        self.assertEqual(binding["status"], "EXPLICIT_PROVIDER_FALLBACK")
        self.assertEqual(binding["provider_id"], "camera.femto_bolt")
        self.assertIn("advisory binding unavailable", binding["reason"])

    async def test_camera_binding_is_revalidated_before_capture(self) -> None:
        manager = _BindingManager()
        skill = PointingIdentificationSkill(
            capture=None,  # type: ignore[arg-type]
            model="test-model",
            manager=manager,  # type: ignore[arg-type]
            fallback_camera_provider_id="camera.femto_bolt",
        )

        binding = await skill._bind_camera("skill-1")

        self.assertEqual(binding["validity"], "CURRENT")
        self.assertEqual(manager.revalidation_count, 1)

    async def test_cold_camera_returns_actionable_result_without_capture(
        self,
    ) -> None:
        skill = PointingIdentificationSkill(
            capture=None,  # type: ignore[arg-type]
            model="test-model",
            manager=_ColdBindingManager(),  # type: ignore[arg-type]
            fallback_camera_provider_id="camera.femto_bolt",
        )

        result = json.loads(await skill.run("What is visible?"))

        self.assertEqual(result["status"], "PROVIDER_ACTIVATION_REQUIRED")
        self.assertEqual(result["provider_id"], "camera.femto_bolt")
        self.assertFalse(result["physical_action_submitted"])
        self.assertEqual(
            result["developer_activation_url"],
            "http://127.0.0.1:7001/developer/provider/camera.femto_bolt",
        )


if __name__ == "__main__":
    unittest.main()
