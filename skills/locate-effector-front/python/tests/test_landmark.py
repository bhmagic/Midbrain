from __future__ import annotations

import json
import unittest

import numpy as np

from locate_effector_front import (
    parse_effector_front_vlm_result,
    resolve_effector_front_reference,
)


def result(
    *,
    configuration: str = "MOUNTED_TOOL",
    geometry: str = "SINGLE_POINT",
    fallback: str = "NONE",
    points: list[dict] | None = None,
) -> dict:
    return {
        "schema": "physical_agent.effector_front_landmark_vlm",
        "schema_version": 1,
        "scene_suitable": True,
        "reason": "The selected pixels are the distal rigid front with depth.",
        "effector_configuration": configuration,
        "front_geometry": geometry,
        "depth_fallback_reason": fallback,
        "front_points": points
        or [
            {
                "point_id": "front",
                "registered_depth_pixel_yx": [5, 5],
                "confidence": 0.94,
                "selected_surface": "TOOL_TIP",
                "selection_reason": "Distal valid-depth tool surface.",
            }
        ],
    }


def resolve(value: dict, depth: np.ndarray) -> dict:
    return resolve_effector_front_reference(
        vlm_result=value,
        registered_depth_m=depth,
        intrinsics={"fx": 100.0, "fy": 100.0, "cx": 5.0, "cy": 5.0},
        target_from_camera=np.eye(4),
        observed_at_us=123,
        source_frame="camera_registered_depth",
        target_frame="stationary_world",
        calibration_revision="calibration-1",
        route_provenance={"route_id": "generic", "boot_id": "boot-1"},
    )


class EffectorFrontLandmarkTests(unittest.TestCase):
    def test_reflective_tip_can_select_distal_valid_handle_point(self) -> None:
        parsed = parse_effector_front_vlm_result(
            json.dumps(
                result(
                    fallback="REFLECTIVE_FRONT_MISSING_DEPTH",
                    points=[
                        {
                            "point_id": "front",
                            "registered_depth_pixel_yx": [5, 4],
                            "confidence": 0.91,
                            "selected_surface": "TOOL_BODY_OR_HANDLE",
                            "selection_reason": (
                                "The shiny distal tip has no depth; this is "
                                "the most distal valid point on the same tool."
                            ),
                        }
                    ],
                )
            ),
            registered_depth_grid=(11, 11),
        )

        self.assertEqual(
            parsed["depth_fallback_reason"],
            "REFLECTIVE_FRONT_MISSING_DEPTH",
        )
        self.assertEqual(
            parsed["front_points"][0]["selected_surface"],
            "TOOL_BODY_OR_HANDLE",
        )

    def test_paired_gripper_reference_averages_registered_3d_points(self) -> None:
        depth = np.full((11, 11), np.nan)
        depth[5, 3] = 0.5
        depth[5, 7] = 0.6
        value = result(
            configuration="BARE_GRIPPER",
            geometry="PAIRED_POINTS",
            points=[
                {
                    "point_id": "front_1",
                    "registered_depth_pixel_yx": [5, 3],
                    "confidence": 0.95,
                    "selected_surface": "GRIPPER_TIP",
                    "selection_reason": "First jaw distal point.",
                },
                {
                    "point_id": "front_2",
                    "registered_depth_pixel_yx": [5, 7],
                    "confidence": 0.96,
                    "selected_surface": "GRIPPER_TIP",
                    "selection_reason": "Second jaw distal point.",
                },
            ],
        )

        resolved = resolve(value, depth)

        self.assertTrue(resolved["eligible_for_control_math"])
        self.assertEqual(
            resolved["control_reference"]["method"],
            "MEAN_OF_PAIRED_3D_POINTS",
        )
        self.assertTrue(
            np.allclose(
                resolved["control_reference"]["target_point_m"],
                [0.001, 0.0, 0.55],
            )
        )

    def test_exact_depth_pixel_is_required_without_silent_neighbor_snap(self) -> None:
        depth = np.full((11, 11), np.nan)
        depth[5, 6] = 0.7

        with self.assertRaisesRegex(RuntimeError, "no valid exact depth"):
            resolve(result(), depth)

    def test_paired_gripper_requires_both_front_points(self) -> None:
        value = result(
            configuration="BARE_GRIPPER",
            geometry="PAIRED_POINTS",
            points=[
                {
                    "point_id": "front_1",
                    "registered_depth_pixel_yx": [5, 3],
                    "confidence": 0.95,
                    "selected_surface": "GRIPPER_TIP",
                    "selection_reason": "Only one jaw was returned.",
                }
            ],
        )

        with self.assertRaisesRegex(RuntimeError, "front_1 and front_2"):
            resolve(value, np.ones((11, 11)))

    def test_unsuitable_scene_requires_explicit_uncertain_state(self) -> None:
        value = result()
        value["scene_suitable"] = False
        value["front_points"] = []

        with self.assertRaisesRegex(RuntimeError, "explicit uncertain state"):
            resolve(value, np.ones((11, 11)))

    def test_low_confidence_is_not_eligible_for_control_math(self) -> None:
        value = result()
        value["front_points"][0]["confidence"] = 0.5

        resolved = resolve(value, np.ones((11, 11)))

        self.assertEqual(resolved["status"], "REJECTED_OBSERVATION")
        self.assertFalse(resolved["eligible_for_control_math"])
        self.assertIn("confidence", resolved["quality_reasons"][0])

    def test_valid_region_is_enforced_on_depth_grid(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "valid region"):
            resolve_effector_front_reference(
                vlm_result=result(),
                registered_depth_m=np.ones((11, 11)),
                intrinsics={
                    "fx": 100.0,
                    "fy": 100.0,
                    "cx": 5.0,
                    "cy": 5.0,
                },
                target_from_camera=np.eye(4),
                observed_at_us=123,
                source_frame="camera_registered_depth",
                target_frame="stationary_world",
                calibration_revision="calibration-1",
                route_provenance={"route_id": "generic"},
                valid_region={"x": 0, "y": 0, "width": 4, "height": 4},
            )


if __name__ == "__main__":
    unittest.main()
