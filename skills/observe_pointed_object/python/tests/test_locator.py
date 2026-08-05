from __future__ import annotations

import unittest

import numpy as np

from observe_pointed_object import (
    parse_item_landmark_vlm_result,
    resolve_item_location,
)


def _vlm(*, material: str = "OPAQUE_DIFFUSE") -> dict[str, object]:
    return {
        "schema": "physical_agent.item_landmark_vlm",
        "schema_version": 1,
        "scene_suitable": True,
        "reason": "visible target surface",
        "item_label": "toilet paper roll",
        "confidence": 0.94,
        "material_class": material,
        "registered_depth_pixel_yx": [4, 5],
        "registered_depth_box_yxyx": [2, 2, 8, 9],
        "same_surface_search_allowed": True,
    }


def _resolve(depth: np.ndarray, **kwargs):
    return resolve_item_location(
        vlm_result=_vlm(material=kwargs.pop("material", "OPAQUE_DIFFUSE")),
        registered_depth_m=depth,
        intrinsics={"fx": 100.0, "fy": 100.0, "cx": 5.0, "cy": 4.0},
        target_from_camera=np.eye(4),
        observed_at_us=123,
        source_frame="camera",
        target_frame="rebot_arm_base",
        calibration_revision="cal-1",
        route_provenance={"route_id": "generic"},
        object_id="test-roll",
        contact_policy="NO_CONTACT",
        **kwargs,
    )


class ItemLocatorTests(unittest.TestCase):
    def test_normalized_vlm_geometry_maps_to_registered_depth_pixels(self):
        result = parse_item_landmark_vlm_result(
            '{"schema":"physical_agent.item_landmark_vlm",'
            '"schema_version":2,'
            '"coordinate_space":"NORMALIZED_0_1000",'
            '"scene_suitable":true,"reason":"visible roll",'
            '"item_label":"toilet paper roll","confidence":0.95,'
            '"material_class":"OPAQUE_DIFFUSE",'
            '"registered_depth_pixel_yx":[678,589],'
            '"registered_depth_box_yxyx":[588,546,772,633],'
            '"same_surface_search_allowed":true}',
            registered_depth_grid=(1080, 1920),
        )

        self.assertEqual(result["registered_depth_pixel_yx"], [732, 1130])
        self.assertEqual(
            result["registered_depth_box_yxyx"],
            [635, 1048, 834, 1216],
        )
        self.assertEqual(
            result["source_coordinate_space"],
            "NORMALIZED_0_1000",
        )

    def test_opaque_item_uses_supported_exact_depth(self):
        depth = np.full((10, 12), 1.2, dtype=np.float64)
        depth[2:8, 2:9] = 0.7

        result = _resolve(depth)

        self.assertEqual(result["status"], "METRIC_POINT_READY")
        self.assertEqual(result["metric_source"], "REGISTERED_DEPTH_EXACT")
        self.assertEqual(result["object_id"], "test-roll")
        self.assertEqual(result["semantic_role"], "WORKPIECE")
        self.assertEqual(result["contact_policy"], "NO_CONTACT")
        self.assertAlmostEqual(result["location"]["target_point_m"][2], 0.7)
        volume = result["volume_hint"]
        self.assertEqual(
            volume["method"],
            "FRONT_SURFACE_PROJECTED_CROSS_SECTION_CENTROID_V1",
        )
        self.assertAlmostEqual(volume["width_m"], 0.049)
        self.assertAlmostEqual(volume["height_m"], 0.042)
        self.assertAlmostEqual(
            volume["representative_sphere_radius_m"],
            0.021,
        )
        self.assertAlmostEqual(
            volume["estimated_centroid_target_m"][2],
            0.721,
        )
        self.assertLess(
            volume["representative_sphere_radius_m"],
            0.5 * np.hypot(volume["width_m"], volume["height_m"]),
        )

    def test_missing_exact_depth_uses_foreground_neighbor_inside_item_box(self):
        depth = np.full((10, 12), 1.4, dtype=np.float64)
        depth[2:8, 2:9] = 0.72
        depth[4, 5] = 0.0

        result = _resolve(depth)

        self.assertEqual(
            result["metric_source"],
            "REGISTERED_DEPTH_SAME_SURFACE_NEIGHBOR",
        )
        self.assertAlmostEqual(result["location"]["target_point_m"][2], 0.72)

    def test_transparent_item_uses_task_plane_instead_of_background_depth(self):
        depth = np.full((10, 12), 2.5, dtype=np.float64)

        result = _resolve(
            depth,
            material="TRANSPARENT",
            task_plane={
                "plane_id": "current-effector-altitude",
                "normal_target": [0.0, 0.0, 1.0],
                "offset_m": 0.8,
                "uncertainty_m": 0.02,
            },
        )

        self.assertEqual(result["metric_source"], "TASK_PLANE_INTERSECTION")
        self.assertIsNone(result["depth_evidence"])
        self.assertAlmostEqual(result["location"]["target_point_m"][2], 0.8)
        self.assertEqual(
            result["degraded_reason"],
            "MATERIAL_LIMITED_DEPTH_USING_TASK_PLANE",
        )

    def test_reflective_item_returns_bearing_only_when_no_plane_exists(self):
        depth = np.full((10, 12), 2.5, dtype=np.float64)

        result = _resolve(depth, material="REFLECTIVE")

        self.assertEqual(result["status"], "BEARING_ONLY")
        self.assertFalse(result["eligible_for_control_math"])
        self.assertIsNone(result["location"])
        self.assertEqual(
            result["recommended_next_action"],
            "ACQUIRE_SECOND_VIEW_OR_USE_BOUNDED_IMAGE_SERVO",
        )

    def test_require_metric_rejects_bearing_only_without_erasing_evidence(self):
        depth = np.zeros((10, 12), dtype=np.float64)

        result = _resolve(depth, depth_requirement="REQUIRE_METRIC")

        self.assertEqual(result["status"], "REJECTED_OBSERVATION")
        self.assertIn("METRIC_LOCATION_REQUIRED", result["quality_reasons"])
        self.assertIsNotNone(result["bearing"])


if __name__ == "__main__":
    unittest.main()
