from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest
from jsonschema import validate as validate_json

pytest.importorskip("agents")
from agents.tool import validate_responses_tool_search_configuration

from physical_agent_test.agent_driver import (
    LIMITED_GRAPH_AGENT_GUIDANCE,
    LIMITED_GRAPH_ROUTED_REMINDER,
    PrototypeAgentDriver,
    _resolve_workspace_root,
    _select_routed_tools,
    deterministic_intent_tool_route,
)
from physical_agent_test.gemini_pointing_skill import PointingIdentificationSkill
from physical_agent_test.skill_catalog import (
    declared_schema_pointers,
    discover_agent_skills,
)
from physical_agent_test.skill_execution import build_agent_tools


class _PointingSkill:
    async def run(self, question: str) -> str:
        return json.dumps(
            {
                "answer": question,
                "confidence": "high",
                "annotations": [],
            }
        )


class _EffectorFrontSkill:
    async def run(self, *, target_frame: str) -> dict[str, object]:
        return {
            "schema": "physical_agent.effector_front_reference",
            "schema_version": 1,
            "status": "REFERENCE_READY",
            "eligible_for_control_math": True,
            "motion_usable": False,
            "publishes_control_frame": False,
            "specialized_action_point": False,
            "observed_at_us": 1,
            "source_frame": "camera_optical",
            "target_frame": target_frame,
            "calibration_revision": None,
            "effector_configuration": "SINGLE_FRONT",
            "front_geometry": "SINGLE_POINT",
            "depth_fallback_reason": None,
            "front_points": [],
            "control_reference": {
                "method": "SINGLE_REGISTERED_3D_POINT",
                "target_point_m": [0.1, 0.2, 0.3],
                "pair_separation_m": None,
            },
            "quality_reasons": [],
            "vlm_reason": "",
            "data_route": {},
            "skill_id": "locate-effector-front-test",
            "safety_class": "READ_ONLY",
            "physical_action_submitted": False,
            "control_frame_published": False,
            "capability_binding": {},
            "binding_mode": "SHADOW",
            "generic_route_mode": "SHADOW",
            "camera_capture": {},
            "transform_provenance": {},
            "selected_route_metadata": {},
            "source_convention_id": "CAMERA_OPTICAL_X_RIGHT_Y_DOWN_Z_FORWARD_V1",
            "target_convention_id": "MIDBRAIN_X_FORWARD_Y_LEFT_Z_UP_V2",
            "vlm_route": {},
            "vlm_geometry": {},
            "vlm_evidence": {},
            "evidence_image": None,
            "visual_evidence": None,
            "input_temporal_evidence": {},
        }


class _ExternalSkillAdapter:
    async def invoke(self, arguments: dict[str, object]) -> str:
        return json.dumps(
            {
                "schema": "midbrain.arm_root_translation_refinement",
                "schema_version": 1,
                "status": "OBSERVATION_ONLY",
                "workflow_complete": True,
                "eligible_for_state_update": False,
                "adoption_factor": arguments.get("adoption_factor", 1.0),
                "landmark_id": (
                    arguments.get("landmark_id") or "default-landmark"
                ),
                "multi_sample_refinement": {
                    "requested_sample_count": arguments.get(
                        "sample_count", 1
                    )
                },
                "landmark_depth_reselection": {},
                "physical_motion_authorized": False,
                "physical_motion_submitted": False,
            }
        )


class _ItemLocatorSkill:
    async def run(
        self,
        *,
        question: str,
        target_frame: str,
        object_id: str | None,
        contact_policy: str,
        depth_requirement: str,
        task_plane: dict[str, object] | None,
    ) -> dict[str, object]:
        return {
            "schema": "physical_agent.item_location",
            "schema_version": 1,
            "status": "REJECTED_OBSERVATION",
            "eligible_for_control_math": False,
            "motion_usable": False,
            "target_frame": target_frame,
            "object_id": object_id or "item-test",
            "item_label": question,
            "semantic_role": "WORKPIECE",
            "contact_policy": contact_policy,
            "quality_reasons": ["TEST_FIXTURE"],
            "vlm_reason": "test fixture",
            "skill_id": "locate-item-test",
            "safety_class": "READ_ONLY",
            "physical_action_submitted": False,
            "control_frame_published": False,
            "capability_binding": {},
            "binding_mode": "SHADOW",
            "generic_route_mode": "SHADOW",
            "camera_capture": {},
            "transform_provenance": {},
            "selected_route_metadata": {},
            "source_convention_id": "CAMERA_OPTICAL_X_RIGHT_Y_DOWN_Z_FORWARD_V1",
            "target_convention_id": "MIDBRAIN_X_FORWARD_Y_LEFT_Z_UP_V2",
            "vlm_route": {},
            "vlm_geometry": {},
            "vlm_evidence": {},
            "visual_box_support": {},
            "evidence_image": None,
            "visual_evidence": None,
            "input_temporal_evidence": {},
            "semantic_scene_assertion": {"status": "NOT_CONFIGURED"},
        }


class _ScenePolicyPublisher:
    async def publish_policy(self, **arguments):
        return {
            "status": "PUBLISHED",
            "arguments": arguments,
        }


class _NoContactApproachSkill:
    async def run(self, **arguments):
        return {
            "schema": "physical_agent.no_contact_item_correction_plan",
            "schema_version": 1,
            "status": "PLAN_INPUT_REJECTED",
            "workflow_complete": False,
            **arguments,
            "physical_motion_authorized": False,
            "motion_submitted": False,
            "target_frame": "rebot_arm_base",
            "contact_policy": {
                "behavior": "NO_CONTACT",
                "allowed_contact_object_ids": [],
                "permit_pushable_contact": False,
            },
            "next_action": "REOBSERVE_BOTH_AND_REPLAN",
            "source_evidence": {},
        }

    async def execute_current_preview(self):
        return {"status": "COMPLETED"}

    async def begin_agent_turn(self):
        return None


class _ReviewedExecutionSkill:
    async def run(self, *, decision_id: str) -> dict[str, object]:
        return {
            "schema": "physical_agent.reviewed_observation_motion_execution",
            "schema_version": 1,
            "decision_id": decision_id,
            "status": "COMPLETED",
            "approval_executes_action": False,
            "model_supplied_motion_parameters": False,
            "reviewed_scene_refreshed": True,
            "scene_revision": "scene-test",
            "integrated_controller": {},
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
    def test_discovery_rejects_missing_mandatory_output_schema(self) -> None:
        workspace = Path(__file__).resolve().parents[3]
        manifest = json.loads(
            (workspace / "skills" / "slicing" / "manifest.json").read_text(
                encoding="utf-8"
            )
        )
        manifest["agent_discovery"].pop("output_schema")

        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            skill_directory = temporary_root / "skills" / "missing-output"
            skill_directory.mkdir(parents=True)
            (skill_directory / "manifest.json").write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError,
                "output_schema must be an object",
            ):
                discover_agent_skills(temporary_root, include_disabled=True)

    def test_workspace_root_uses_launcher_environment_when_installed(self) -> None:
        workspace = Path(__file__).resolve().parents[3]
        with patch.dict(
            os.environ,
            {"PHYSICAL_AGENT_ROOT": str(workspace)},
        ):
            self.assertEqual(_resolve_workspace_root(None), workspace.resolve())

    def test_explicit_world_axis_only_route_excludes_arm_calibration(self) -> None:
        route = deterministic_intent_tool_route(
            "establish the world axis (not the arm base)"
        )

        self.assertEqual(route["route"], "WORLD_AXIS_ONLY")
        self.assertNotIn(
            "calibrate_stationary_workcell",
            route["allowed_tools"],
        )
        self.assertIn("inspect_midbrain_runtime", route["allowed_tools"])
        self.assertIn("establish_world_axis", route["allowed_tools"])
        self.assertIn("tool_search", route["allowed_tools"])
        self.assertNotIn("set_provider_residency", route["allowed_tools"])

    def test_routed_deferred_tools_retain_required_search_infrastructure(
        self,
    ) -> None:
        driver = PrototypeAgentDriver(
            _PointingSkill(),
            "gpt-test",
            eligible_tool_names={"identify_pointed_object"},
            defer_loading=True,
        )

        selected = _select_routed_tools(
            driver.agent.tools,
            {"identify_pointed_object"},
        )

        self.assertEqual(
            [getattr(tool, "name", "") for tool in selected],
            ["identify_pointed_object", "tool_search"],
        )
        validate_responses_tool_search_configuration(selected)

    def test_limited_graph_stays_immediate_when_child_schemas_are_deferred(
        self,
    ) -> None:
        workspace = Path(__file__).resolve().parents[3]
        descriptor = next(
            item
            for item in discover_agent_skills(workspace)
            if item.tool_name == "run_limited_graph"
        )
        tools = build_agent_tools(
            [descriptor],
            {descriptor.execution_adapter_id: _ExternalSkillAdapter()},
            eligible_tool_names={"run_limited_graph"},
            defer_loading=True,
        )

        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0].name, "run_limited_graph")
        self.assertFalse(tools[0].defer_loading)

    def test_limited_graph_guidance_prefers_graph_before_direct_children(
        self,
    ) -> None:
        self.assertIn(
            "Strongly prefer run_limited_graph",
            LIMITED_GRAPH_AGENT_GUIDANCE,
        )
        self.assertIn(
            "before invoking any graph child directly",
            LIMITED_GRAPH_AGENT_GUIDANCE,
        )
        self.assertIn(
            "Never invoke a failed graph child",
            LIMITED_GRAPH_AGENT_GUIDANCE,
        )
        self.assertIn(
            "fresh request unless the user explicitly asks to resume",
            LIMITED_GRAPH_AGENT_GUIDANCE,
        )
        self.assertIn(
            "do not begin a direct multi-Skill sequence",
            LIMITED_GRAPH_AGENT_GUIDANCE,
        )
        self.assertIn(
            "every requested graph-eligible stage",
            LIMITED_GRAPH_AGENT_GUIDANCE,
        )
        self.assertIn(
            "never submit only a prefix",
            LIMITED_GRAPH_AGENT_GUIDANCE,
        )
        self.assertIn(
            "after the route-specific instructions",
            LIMITED_GRAPH_ROUTED_REMINDER,
        )
        self.assertIn(
            "Do not submit a prefix-only graph",
            LIMITED_GRAPH_ROUTED_REMINDER,
        )
        self.assertIn(
            "do not treat a tool-search result as graph execution",
            LIMITED_GRAPH_ROUTED_REMINDER,
        )

    def test_explicit_3d_item_route_excludes_rgb_only_analysis(self) -> None:
        route = deterministic_intent_tool_route(
            "identify the white object on the table center and 3d locate it"
        )

        self.assertEqual(route["route"], "METRIC_ITEM_LOCATION")
        self.assertIn("locate_item", route["allowed_tools"])
        self.assertIn("inspect_arm_semantic_scene", route["allowed_tools"])
        self.assertNotIn("analyze_visual_scene", route["allowed_tools"])

    def test_workpiece_corner_route_uses_fresh_arm_base_aabb(self) -> None:
        route = deterministic_intent_tool_route(
            "where is the right-forward-up corner of the work piece"
        )

        self.assertEqual(route["route"], "SEMANTIC_WORK_OBJECT_BOUNDS")
        self.assertIn("inspect_arm_semantic_scene", route["allowed_tools"])
        self.assertNotIn("locate_item", route["allowed_tools"])
        self.assertIn("forward is +X", route["instruction"])
        self.assertIn("right is -Y", route["instruction"])
        self.assertIn("currently visible surface", route["instruction"])
        self.assertIn("never authorizes movement", route["instruction"])

    def test_workpiece_corner_motion_uses_world_point_tandem_route(self) -> None:
        route = deterministic_intent_tool_route(
            "move the hand to the work piece right-forward-up corner plus "
            "(0, -2, 15) cm in the arm base axes"
        )

        self.assertEqual(
            route["route"],
            "SEMANTIC_WORK_OBJECT_WORLD_POINT_MOTION",
        )
        self.assertIn(
            "derive_fabric_world_point",
            route["allowed_tools"],
        )
        self.assertIn(
            "move_effector_to_world_point",
            route["allowed_tools"],
        )
        self.assertNotIn(
            "inspect_arm_semantic_scene",
            route["allowed_tools"],
        )
        self.assertNotIn("tool_search", route["allowed_tools"])
        self.assertNotIn(
            "perform_relative_effector_motion",
            route["allowed_tools"],
        )
        self.assertIn(
            "Never perform coordinate addition",
            route["instruction"],
        )
        self.assertIn(
            "copying target_position_world_m",
            route["instruction"],
        )
        self.assertIn(
            "single authoritative scene read",
            route["instruction"],
        )
        self.assertIn(
            "Do not call inspect_arm_semantic_scene",
            route["instruction"],
        )

    def test_scene_mapping_and_corner_motion_use_composed_tandem_route(
        self,
    ) -> None:
        route = deterministic_intent_tool_route(
            "map out the working piece: toilet paper roll; map out the "
            "obstacle: table surface; move the hand to (0, -2, 15) cm in "
            "arm base axes from the work piece right-forward-up corner"
        )

        self.assertEqual(
            route["route"],
            "SCENE_POLICY_AND_WORK_OBJECT_WORLD_POINT_MOTION",
        )
        self.assertIn(
            "configure_scene_policy_and_inspect_runtime",
            route["allowed_tools"],
        )
        self.assertNotIn("inspect_midbrain_runtime", route["allowed_tools"])
        self.assertIn(
            "derive_fabric_world_point",
            route["allowed_tools"],
        )
        self.assertIn(
            "move_effector_to_world_point",
            route["allowed_tools"],
        )
        self.assertNotIn(
            "inspect_arm_semantic_scene",
            route["allowed_tools"],
        )
        self.assertNotIn("tool_search", route["allowed_tools"])
        self.assertNotIn(
            "execute_no_contact_approach_step",
            route["allowed_tools"],
        )
        self.assertIn(
            "null expected_scene_revision",
            route["instruction"],
        )
        self.assertIn(
            "Do not use no-contact approach tools",
            route["instruction"],
        )

    def test_scene_corner_motion_and_slicing_expose_complete_graph_surface(
        self,
    ) -> None:
        route = deterministic_intent_tool_route(
            "map out the working piece: toilet paper; map out the obstacle: "
            "white table surface; move the hand to (0, -2, 15) cm in arm "
            "base axes from the right-forward-up corner; use current IK "
            "+(0,0,-10) cm for a slice with world -z blade direction and arm "
            "base -x slicing direction; move above the first slice and slice "
            "again"
        )

        self.assertEqual(
            route["route"],
            "SCENE_CORNER_MOTION_AND_MIXED_FRAME_SLICING",
        )
        self.assertIn(
            "configure_scene_policy_and_inspect_runtime",
            route["allowed_tools"],
        )
        self.assertNotIn("inspect_midbrain_runtime", route["allowed_tools"])
        self.assertIn(
            "derive_fabric_world_point",
            route["allowed_tools"],
        )
        self.assertIn(
            "move_effector_to_world_point",
            route["allowed_tools"],
        )
        self.assertIn(
            "translate_fabric_direction_to_world",
            route["allowed_tools"],
        )
        self.assertIn("slice_with_blade", route["allowed_tools"])
        self.assertIn("one complete Limited Graph", route["instruction"])
        self.assertIn("never submit a corner-move prefix", route["instruction"])
        self.assertIn("never retry or loop a physical node", route["instruction"])
        self.assertIn(
            "/plan/path/slice_begin_point_world_m",
            route["instruction"],
        )
        self.assertIn(
            "never substitute /plan/path/planned_retract_endpoint_world_m",
            route["instruction"],
        )

    def test_existing_scene_corner_motion_and_slicing_share_graph_route(
        self,
    ) -> None:
        route = deterministic_intent_tool_route(
            "move the hand to (0, -2, 15) cm in arm base axes from the "
            "toilet paper right-forward-up corner; use current IK "
            "+(0,0,-10) cm as the beginning point of cutting, world -z as "
            "the blade direction, arm base -x as the slicing direction, and "
            "submit a 20 cm slice using default profiles"
        )

        self.assertEqual(
            route["route"],
            "WORK_OBJECT_MOTION_AND_MIXED_FRAME_SLICING",
        )
        self.assertIn(
            "derive_fabric_world_point",
            route["allowed_tools"],
        )
        self.assertIn(
            "move_effector_to_world_point",
            route["allowed_tools"],
        )
        self.assertIn(
            "translate_fabric_direction_to_world",
            route["allowed_tools"],
        )
        self.assertIn("inspect_arm_semantic_scene", route["allowed_tools"])
        self.assertIn("offset_world_point", route["allowed_tools"])
        self.assertIn("inspect_arm_semantic_scene", route["allowed_tools"])
        self.assertIn("offset_world_point", route["allowed_tools"])
        self.assertIn("slice_with_blade", route["allowed_tools"])
        self.assertNotIn(
            "configure_scene_segmentation_policy",
            route["allowed_tools"],
        )
        self.assertIn("one complete Limited Graph", route["instruction"])
        self.assertIn("never submit only", route["instruction"])

    def test_safe_home_has_an_exact_host_operation_route(self) -> None:
        route = deterministic_intent_tool_route("safe home")

        self.assertEqual(route["route"], "SAFE_HOME")
        self.assertFalse(route["allow_limited_graph"])
        self.assertIn("execute_basic_safe_home", route["allowed_tools"])
        self.assertIn(
            "Call execute_basic_safe_home directly",
            route["instruction"],
        )
        self.assertIn("not a Limited Graph child", route["instruction"])

    def test_compound_graph_route_preserves_trailing_safe_home(self) -> None:
        route = deterministic_intent_tool_route(
            "map out the working piece and obstacle; move the hand to the "
            "right-forward-up corner; use current IK for arm base -x slicing; "
            "move above the first slice, slice again, then safe home"
        )

        self.assertEqual(
            route["route"],
            "SCENE_CORNER_MOTION_AND_MIXED_FRAME_SLICING",
        )
        self.assertIn("execute_basic_safe_home", route["allowed_tools"])
        self.assertIn("directly only after the graph", route["instruction"])

    def test_mixed_frame_slicing_uses_direction_translation_tandem(self) -> None:
        route = deterministic_intent_tool_route(
            "use current IK +(0,0,-10)cm as beginning, world -z blade, "
            "arm base -x slicing, 10cm default"
        )

        self.assertEqual(route["route"], "MIXED_FRAME_SLICING")
        self.assertIn(
            "translate_fabric_direction_to_world",
            route["allowed_tools"],
        )
        self.assertIn("slice_with_blade", route["allowed_tools"])
        self.assertNotIn(
            "translate_fabric_pose_to_world",
            route["allowed_tools"],
        )
        self.assertIn(
            "copy direction_world unchanged",
            route["instruction"],
        )
        self.assertIn(
            "point_mode=RELATIVE_TO_CURRENT_EFFECTOR_WORLD",
            route["instruction"],
        )

    def test_move_close_route_uses_composed_no_contact_planner(self) -> None:
        route = deterministic_intent_tool_route(
            "can you move the gripper to close to the toilet paper roll"
        )

        self.assertEqual(route["route"], "NO_CONTACT_ITEM_APPROACH")
        self.assertIn(
            "plan_no_contact_item_approach",
            route["allowed_tools"],
        )
        self.assertIn(
            "execute_no_contact_approach_step",
            route["allowed_tools"],
        )
        self.assertNotIn(
            "preview_relative_effector_motion",
            route["allowed_tools"],
        )
        self.assertIn("measured_arrival_confirmed=true", route["instruction"])
        self.assertIn(
            "do not report WAITING_NEXT as unconfirmed motion",
            route["instruction"],
        )

    def test_move_above_item_with_implicit_effector_uses_approach(self) -> None:
        route = deterministic_intent_tool_route(
            "move the to above the toilet paper roll without changing the "
            "height or direction"
        )

        self.assertEqual(route["route"], "NO_CONTACT_ITEM_APPROACH")
        self.assertIn(
            "plan_no_contact_item_approach",
            route["allowed_tools"],
        )
        self.assertNotIn(
            "configure_scene_segmentation_policy",
            route["allowed_tools"],
        )
        self.assertIn("do not invent one", route["instruction"])

    def test_move_until_work_object_uses_no_contact_approach(self) -> None:
        route = deterministic_intent_tool_route(
            "move the hand down 50 mm five times until reaching the work object"
        )

        self.assertEqual(route["route"], "NO_CONTACT_ITEM_APPROACH")
        self.assertIn(
            "COMPLETED_CLOSEST_SAFE",
            route["instruction"],
        )
        self.assertIn("boundary target", route["instruction"])
        self.assertIn("neither contact authorization", route["instruction"])
        self.assertIn("zero extra WORK_OBJECT clearance", route["instruction"])
        self.assertIn("10 mm", route["instruction"])

    def test_explicit_obstacle_route_uses_declared_scene_policy(self) -> None:
        route = deterministic_intent_tool_route(
            "The only obstacle is the table; do not collide with it."
        )

        self.assertEqual(
            route["route"],
            "EXPLICIT_SCENE_SEGMENTATION_POLICY",
        )
        self.assertIn(
            "configure_scene_policy_and_inspect_runtime",
            route["allowed_tools"],
        )
        self.assertNotIn("inspect_midbrain_runtime", route["allowed_tools"])
        self.assertIn(
            "inspect_arm_semantic_scene",
            route["allowed_tools"],
        )
        self.assertNotIn(
            "perception.sam2_scene_tracker HOT",
            route["instruction"],
        )
        self.assertIn(
            "world_model.arm_scene_compiler",
            route["instruction"],
        )
        self.assertIn(
            "required_capability=world_model.arm.semantic_scene",
            route["instruction"],
        )
        self.assertIn(
            "Manager owns transitive activation",
            route["instruction"],
        )
        self.assertIn("SCENE_READY", route["instruction"])

    def test_catalog_exposes_only_discoverable_skills_by_default(self) -> None:
        workspace = Path(__file__).resolve().parents[3]

        descriptors = discover_agent_skills(workspace)

        self.assertEqual(
            [descriptor.tool_name for descriptor in descriptors],
            [
                "analyze_visual_scene",
                "calibrate_stationary_workcell",
                "derive_fabric_world_point",
                "establish_world_axis",
                "execute_reviewed_observation_motion",
                "identify_pointed_object",
                "inspect_arm_semantic_scene",
                "locate_effector_front",
                "locate_item",
                "move_effector_to_world_point",
                "offset_world_point",
                "perform_relative_effector_motion",
                "plan_no_contact_item_approach",
                "refine_arm_root_translation",
                "register_rgbd_pixel_to_world",
                "register_tool_to_control_frame",
                "reinitialize_space_cognition",
                "run_limited_graph",
                "slice_with_blade",
                "translate_fabric_direction_to_world",
                "translate_fabric_pose_to_world",
                "verify_rgbd_image_alignment",
            ],
        )
        relative_motion = next(
            descriptor
            for descriptor in descriptors
            if descriptor.tool_name == "perform_relative_effector_motion"
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
        self.assertNotIn(
            "localization.vio.local_pose",
            relative_motion.required_capabilities,
        )
        self.assertIn(
            "localization.vio.local_pose",
            relative_motion.optional_capabilities,
        )
        self.assertFalse(
            relative_motion.route_policy[
                "missing_visual_depth_may_veto_ik_preview"
            ]
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

    def test_all_installed_skills_publish_v3_two_tier_output_schemas(self) -> None:
        workspace = Path(__file__).resolve().parents[3]
        discovery_schema = json.loads(
            (
                workspace
                / "contracts"
                / "schemas"
                / "agent_skill_discovery.v3.schema.json"
            ).read_text(encoding="utf-8")
        )

        descriptors = discover_agent_skills(workspace, include_disabled=True)

        for manifest_path in sorted((workspace / "skills").glob("*/manifest.json")):
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            validate_json(
                instance=manifest["agent_discovery"],
                schema=discovery_schema,
            )

        self.assertEqual(len(descriptors), 23)
        self.assertTrue(all(item.schema_version == 3 for item in descriptors))
        self.assertTrue(
            all(item.output_schema["type"] == "object" for item in descriptors)
        )
        self.assertTrue(
            all(
                "x-midbrain-result-tiers" in item.output_schema
                for item in descriptors
            )
        )
        self.assertTrue(
            all(
                item.result_tiers.detail_policy
                in {"HOST_SANITIZED_REFERENCE", "NONE"}
                for item in descriptors
            )
        )
        slicing = next(
            item for item in descriptors if item.tool_name == "slice_with_blade"
        )
        pointers = declared_schema_pointers(slicing.output_schema)
        self.assertIn("/plan/path/slice_begin_point_world_m", pointers)
        self.assertIn("/plan/path/slice_begin_point_world_m/0", pointers)
        self.assertIn("/plan/path/slice_endpoint_world_m", pointers)
        self.assertIn("/plan/path/slice_endpoint_world_m/0", pointers)
        self.assertIn(
            "/plan/path/planned_retract_endpoint_world_m",
            pointers,
        )
        self.assertIn(
            "/plan/path/planned_retract_endpoint_world_m/0",
            pointers,
        )
        self.assertIn("/plan/workcell_binding/world_frame", pointers)
        self.assertIn(
            "/plan/path/slice_endpoint_world_m",
            slicing.result_tiers.compact_pointers,
        )
        self.assertNotIn("/outward_retract_end_position_world_m", pointers)

    def test_slicing_strict_tool_uses_null_for_live_profile_defaults(self) -> None:
        driver = PrototypeAgentDriver(
            _PointingSkill(),
            "gpt-test",
            tool_choice="required",
            eligible_tool_names={"slice_with_blade"},
            external_skill_adapters={
                "skill.slicing.host.v1": _ExternalSkillAdapter()
            },
        )

        tool = driver.agent.tools[0]
        self.assertEqual(tool.name, "slice_with_blade")
        self.assertIn("blade_profile_number", tool.params_json_schema["required"])
        self.assertIn("motion_profile_number", tool.params_json_schema["required"])
        self.assertEqual(
            tool.params_json_schema["properties"]["blade_profile_number"]["type"],
            ["integer", "null"],
        )
        self.assertEqual(
            tool.params_json_schema["properties"]["motion_profile_number"]["type"],
            ["integer", "null"],
        )

    def test_catalog_excludes_archived_vegetable_cutting_skill(self) -> None:
        workspace = Path(__file__).resolve().parents[3]

        descriptors = discover_agent_skills(workspace, include_disabled=True)
        by_name = {descriptor.tool_name: descriptor for descriptor in descriptors}

        self.assertNotIn("vegetable_cutting_legacy_local", by_name)
        observation = by_name["locate_item"]
        self.assertTrue(observation.discoverable)
        self.assertIsNone(observation.disabled_reason)
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

    async def test_external_refinement_adapter_is_manifest_driven(
        self,
    ) -> None:
        driver = PrototypeAgentDriver(
            _PointingSkill(),
            "gpt-test",
            tool_choice="required",
            eligible_tool_names={"refine_arm_root_translation"},
            external_skill_adapters={
                "skill.refine_arm_root_translation.v1": (
                    _ExternalSkillAdapter()  # type: ignore[dict-item]
                )
            },
        )

        self.assertEqual(
            driver.agent.tools[0].name,
            "refine_arm_root_translation",
        )
        self.assertFalse(driver.agent.tools[0].needs_approval)
        self.assertEqual(driver.agent.tools[0].timeout_seconds, 600.0)
        self.assertEqual(
            set(driver.agent.tools[0].params_json_schema["properties"]),
            {"adoption_factor", "sample_count", "landmark_id"},
        )
        self.assertEqual(
            driver.agent.tools[0].params_json_schema["properties"][
                "landmark_id"
            ]["type"],
            ["string", "null"],
        )
        result = await driver.agent.tools[0].on_invoke_tool(
            None,  # type: ignore[arg-type]
            '{"adoption_factor":0.4}',
        )
        parsed = json.loads(result)
        self.assertEqual(parsed["adoption_factor"], 0.4)
        self.assertEqual(
            parsed["multi_sample_refinement"]["requested_sample_count"],
            1,
        )
        self.assertEqual(parsed["landmark_id"], "default-landmark")
        self.assertFalse(parsed["physical_motion_submitted"])

        defaulted = await driver.agent.tools[0].on_invoke_tool(
            None,  # type: ignore[arg-type]
            '{}',
        )
        defaulted_parsed = json.loads(defaulted)
        self.assertEqual(defaulted_parsed["adoption_factor"], 1.0)
        self.assertEqual(
            defaulted_parsed["multi_sample_refinement"][
                "requested_sample_count"
            ],
            1,
        )
        self.assertEqual(
            defaulted_parsed["landmark_id"], "default-landmark"
        )

        requested = await driver.agent.tools[0].on_invoke_tool(
            None,  # type: ignore[arg-type]
            json.dumps(
                {
                    "adoption_factor": 0.5,
                    "sample_count": 5,
                    "landmark_id": "rail_lateral_endpoint_mean",
                }
            ),
        )
        requested_parsed = json.loads(requested)
        self.assertEqual(requested_parsed["adoption_factor"], 0.5)
        self.assertEqual(
            requested_parsed["multi_sample_refinement"][
                "requested_sample_count"
            ],
            5,
        )
        self.assertEqual(
            requested_parsed["landmark_id"],
            "rail_lateral_endpoint_mean",
        )

    async def test_scene_policy_tool_publishes_exact_described_objects(
        self,
    ) -> None:
        driver = PrototypeAgentDriver(
            _PointingSkill(),
            "gpt-test",
            tool_choice="required",
            eligible_tool_names={"identify_pointed_object"},
            scene_policy_publisher=(
                _ScenePolicyPublisher()  # type: ignore[arg-type]
            ),
        )
        tools = {tool.name: tool for tool in driver.agent.tools}

        result = await tools[
            "configure_scene_segmentation_policy"
        ].on_invoke_tool(
            None,  # type: ignore[arg-type]
            json.dumps(
                {
                    "policy_id": "table-only",
                    "objects": [
                        {
                            "object_id": "table",
                            "type": "KEEP_OUT",
                            "description": "the complete support table",
                        }
                    ],
                    "arm_description": "the complete robot arm",
                }
            ),
        )

        parsed = json.loads(result)
        self.assertEqual(parsed["status"], "PUBLISHED")
        self.assertEqual(
            parsed["arguments"]["objects"][0]["object_id"],
            "table",
        )

    async def test_item_locator_manifest_invokes_read_only_adapter(self) -> None:
        driver = PrototypeAgentDriver(
            _PointingSkill(),
            "gpt-test",
            tool_choice="required",
            eligible_tool_names={"locate_item"},
            item_locator_skill=_ItemLocatorSkill(),  # type: ignore[arg-type]
        )

        self.assertEqual(driver.agent.tools[0].name, "locate_item")
        result = await driver.agent.tools[0].on_invoke_tool(
            None,  # type: ignore[arg-type]
            '{"question":"locate the roll","target_frame":"rebot_arm_base"}',
        )
        parsed = json.loads(result)
        self.assertEqual(parsed["target_frame"], "rebot_arm_base")
        self.assertEqual(parsed["contact_policy"], "WORKPIECE_CONTACT_ALLOWED")
        self.assertFalse(parsed["physical_action_submitted"])

    async def test_no_contact_approach_manifest_invokes_planning_adapter(
        self,
    ) -> None:
        driver = PrototypeAgentDriver(
            _PointingSkill(),
            "gpt-test",
            tool_choice="required",
            eligible_tool_names={"plan_no_contact_item_approach"},
            no_contact_approach_skill=(
                _NoContactApproachSkill()  # type: ignore[arg-type]
            ),
        )

        self.assertEqual(
            driver.agent.tools[0].name,
            "plan_no_contact_item_approach",
        )
        result = await driver.agent.tools[0].on_invoke_tool(
            None,  # type: ignore[arg-type]
            '{"question":"approach the toilet paper"}',
        )
        parsed = json.loads(result)
        self.assertEqual(parsed["requested_standoff_m"], 0.0)
        self.assertEqual(parsed["maximum_step_m"], 1.2)
        self.assertFalse(parsed["physical_motion_authorized"])
        self.assertFalse(parsed["motion_submitted"])

    def test_no_contact_execution_schema_accepts_no_model_plan_identifier(
        self,
    ) -> None:
        driver = PrototypeAgentDriver(
            _PointingSkill(),
            "gpt-test",
            tool_choice="required",
            eligible_tool_names={"plan_no_contact_item_approach"},
            no_contact_approach_skill=(
                _NoContactApproachSkill()  # type: ignore[arg-type]
            ),
        )

        tool = next(
            value
            for value in driver.agent.tools
            if value.name == "execute_no_contact_approach_step"
        )
        self.assertEqual(
            tool.params_json_schema["required"],
            [],
        )
        self.assertEqual(
            tool.params_json_schema["properties"],
            {},
        )
        self.assertFalse(tool.needs_approval)

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

        self.assertEqual(json.loads(result)["answer"], "Which object?")

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
