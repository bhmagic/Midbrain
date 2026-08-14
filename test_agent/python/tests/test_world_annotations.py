import time
import unittest
from unittest.mock import patch

from physical_agent_test import app as app_module


class _WorldPointCloud:
    async def status(self):
        return {
            "world_frame": "rebot_arm_base",
            "session_epoch": "epoch-1",
        }


class _Fabric:
    def __init__(self, scene):
        self.scene = scene

    async def latest_optional(self, stream):
        if stream == "robot_arm.primary.integrated.scene":
            return self.scene
        return None


class WorldAnnotationTests(unittest.IsolatedAsyncioTestCase):
    async def test_visible_aabb_is_a_box_and_dense_spheres_are_unlabeled(self):
        now_us = time.time_ns() // 1000
        corners = {
            "right_forward_up": [0.4, -0.1, 0.3],
            "left_forward_up": [0.4, 0.1, 0.3],
            "right_backward_up": [0.2, -0.1, 0.3],
            "left_backward_up": [0.2, 0.1, 0.3],
            "right_forward_down": [0.4, -0.1, 0.1],
            "left_forward_down": [0.4, 0.1, 0.1],
            "right_backward_down": [0.2, -0.1, 0.1],
            "left_backward_down": [0.2, 0.1, 0.1],
        }
        scene = {
            "observed_at_us": now_us,
            "expires_at_us": now_us + 5_000_000,
            "data": {
                "frame_id": "rebot_arm_base",
                "scene_revision": "scene-1",
                "spheres": [
                    {
                        "sphere_id": f"roll-{index}",
                        "object_id": "toilet_paper_roll",
                        "center_m": [0.3, 0.0, 0.2],
                        "radius_m": 0.02,
                        "type": "WORK_OBJECT",
                    }
                    for index in range(12)
                ]
                + [
                    {
                        "sphere_id": "table-0",
                        "object_id": "table",
                        "center_m": [0.3, 0.0, 0.05],
                        "radius_m": 0.04,
                        "type": "KEEP_OUT",
                    }
                ],
                "visible_surface_aabbs": [
                    {
                        "object_id": "toilet_paper_roll",
                        "description": "toilet paper roll",
                        "type": "WORK_OBJECT",
                        "frame_id": "rebot_arm_base",
                        "center_m": [0.3, 0.0, 0.2],
                        "corners_m": corners,
                        "observed_at_us": now_us,
                        "expires_at_us": now_us + 5_000_000,
                    },
                    {
                        "object_id": "table",
                        "description": "table",
                        "type": "KEEP_OUT",
                        "frame_id": "rebot_arm_base",
                        "center_m": [0.3, 0.0, 0.05],
                        "corners_m": corners,
                        "observed_at_us": now_us,
                        "expires_at_us": now_us + 5_000_000,
                    },
                ],
            },
        }
        with (
            patch.object(app_module, "world_point_cloud", _WorldPointCloud()),
            patch.object(app_module, "fabric", _Fabric(scene)),
            patch.object(
                app_module.item_locator_skill,
                "last_metric_result",
                None,
            ),
        ):
            result = await app_module.world_annotations()

        self.assertEqual(result["schema_version"], 2)
        self.assertEqual(result["box_count"], 1)
        self.assertEqual(result["boxes"][0]["corners_m"], corners)
        self.assertEqual(
            result["boxes"][0]["extent_kind"],
            "VISIBLE_SURFACE_AABB",
        )
        self.assertTrue(result["boxes"][0]["show_label"])
        work_object_spheres = [
            marker
            for marker in result["markers"]
            if marker["type"] == "WORK_OBJECT"
        ]
        self.assertEqual(len(work_object_spheres), 12)
        self.assertTrue(
            all(not marker["show_label"] for marker in work_object_spheres)
        )
        obstacle_markers = [
            marker
            for marker in result["markers"]
            if marker["type"] == "KEEP_OUT"
        ]
        self.assertEqual(len(obstacle_markers), 1)
        self.assertFalse(obstacle_markers[0]["show_label"])
