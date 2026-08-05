from __future__ import annotations

import unittest

import numpy as np

from physical_agent_test.semantic_scene import (
    ARM_BASE_ROI,
    GRIPPER_ROI,
    build_canonical_semantic_scene,
    build_fabric_semantic_scene_observation,
    semantic_object_from_item_location,
)


class SemanticSceneTests(unittest.TestCase):
    def test_raw_points_require_current_self_filter(self):
        with self.assertRaisesRegex(ValueError, "SELF_FILTER_REQUIRED"):
            build_canonical_semantic_scene(
                raw_points_arm_base_m=[[0.3, 0.0, 0.1]],
                roi_scope=ARM_BASE_ROI,
            )

    def test_base_roi_is_limited_and_uses_sixty_millimeter_minimum(self):
        scene = build_canonical_semantic_scene(
            raw_points_arm_base_m=[
                [0.3, 0.0, 0.1],
                [0.305, 0.0, 0.1],
                [1.3, 0.0, 0.0],
            ],
            roi_scope=ARM_BASE_ROI,
            self_exclusion_spheres=[
                {"center_m": [0.0, 0.0, 0.0], "radius_m": 0.1}
            ],
            self_filter_revision="robot-model-1",
        )

        self.assertEqual(scene["roi_layers"][0]["radius_m"], 1.2)
        self.assertEqual(
            scene["roi_layers"][0]["minimum_sphere_radius_m"],
            0.06,
        )
        self.assertEqual(scene["production"]["input_points_in_roi"], 2)
        self.assertTrue(
            all(sphere["radius_m"] >= 0.06 for sphere in scene["spheres"])
        )
        self.assertTrue(
            all(sphere["type"] == "KEEP_OUT" for sphere in scene["spheres"])
        )

    def test_toilet_paper_workpiece_overrides_raw_obstacle_points(self):
        item = {
            "eligible_for_control_math": True,
            "target_frame": "rebot_arm_base",
            "object_id": "toilet-paper",
            "location": {
                "target_point_m": [0.45, 0.0, 0.25],
                "uncertainty_radius_m": 0.01,
            },
            "volume_hint": {"raw_sphere_radius_m": 0.06},
        }
        scene = build_canonical_semantic_scene(
            raw_points_arm_base_m=[
                [0.45, 0.0, 0.25],
                [0.46, 0.0, 0.25],
                [0.7, 0.1, 0.2],
            ],
            roi_scope=GRIPPER_ROI,
            gripper_center_arm_base_m=[0.4, 0.0, 0.25],
            self_exclusion_spheres=[
                {"center_m": [0.4, 0.0, 0.25], "radius_m": 0.02}
            ],
            self_filter_revision="robot-model-2",
            semantic_objects=[semantic_object_from_item_location(item)],
        )

        workpieces = [
            sphere
            for sphere in scene["spheres"]
            if sphere["object_id"] == "toilet-paper"
        ]
        self.assertEqual(len(workpieces), 1)
        self.assertEqual(workpieces[0]["type"], "WORK_OBJECT")
        self.assertGreaterEqual(scene["production"]["semantic_points_removed"], 2)
        self.assertEqual(
            scene["production"]["pushable_requires_explicit_upstream_type"],
            True,
        )

    def test_item_scene_sphere_uses_estimated_volume_centroid(self):
        item = {
            "eligible_for_control_math": True,
            "target_frame": "rebot_arm_base",
            "object_id": "toilet-paper",
            "location": {
                "target_point_m": [0.42, 0.12, 0.17],
                "uncertainty_radius_m": 0.003,
            },
            "volume_hint": {
                "estimated_centroid_target_m": [0.36, 0.14, 0.13],
                "representative_sphere_radius_m": 0.069,
            },
        }

        semantic = semantic_object_from_item_location(item)

        self.assertEqual(semantic["center_m"], [0.36, 0.14, 0.13])
        self.assertEqual(semantic["radius_m"], 0.069)

    def test_gripper_roi_uses_twenty_millimeter_minimum(self):
        scene = build_canonical_semantic_scene(
            raw_points_arm_base_m=np.empty((0, 3)),
            roi_scope=GRIPPER_ROI,
            gripper_center_arm_base_m=[0.4, 0.0, 0.25],
            semantic_objects=[
                {
                    "object_id": "small-workpiece",
                    "center_m": [0.42, 0.0, 0.25],
                    "radius_m": 0.001,
                    "type": "WORKPIECE",
                }
            ],
        )

        self.assertEqual(scene["roi_layers"][0]["radius_m"], 0.5)
        self.assertEqual(scene["spheres"][0]["radius_m"], 0.02)

    def test_fabric_envelope_expires_with_freshness(self):
        scene = build_canonical_semantic_scene(
            raw_points_arm_base_m=np.empty((0, 3)),
            roi_scope=ARM_BASE_ROI,
        )
        observation = build_fabric_semantic_scene_observation(
            scene,
            provider_id="scene.builder",
            provider_instance_id="scene-instance",
            boot_id="scene-boot",
            sequence=4,
            observed_at_us=1_000_000,
            freshness_ms=1000,
        )

        self.assertEqual(
            observation["schema"],
            "physical_agent.arm_semantic_sphere_scene",
        )
        self.assertEqual(observation["expires_at_us"], 2_000_000)


if __name__ == "__main__":
    unittest.main()
