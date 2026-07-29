from __future__ import annotations

import unittest

import numpy as np

from register_tool_to_control_frame import (
    build_control_frame_candidate,
    register_tool_to_control_frame_candidate,
)


class ToolControlFrameRegistrationTests(unittest.TestCase):
    def test_three_landmarks_build_review_only_candidate(self) -> None:
        candidate = build_control_frame_candidate(
            landmarks_target_m={
                "tip": [0.2, 0.0, 0.0],
                "heel": [0.4, 0.0, 0.0],
                "spine": [0.2, 0.0, 0.04],
            },
            target_from_tool=np.eye(4),
            geometry={
                "axis_start_role": "tip",
                "axis_end_role": "heel",
                "plane_role": "spine",
                "origin_from_axis_start_m": 0.12,
                "plane_axis_sign": -1,
                "minimum_landmark_confidence": 0.8,
                "minimum_axis_length_m": 0.1,
                "minimum_plane_offset_m": 0.02,
                "maximum_tool_to_origin_m": 0.5,
            },
            landmark_confidences={"tip": 0.9, "heel": 0.95, "spine": 0.91},
        )

        self.assertEqual(candidate["status"], "CANDIDATE_AUTHORIZATION_REQUIRED")
        self.assertFalse(candidate["motion_usable"])
        self.assertFalse(candidate["publishes_control_frame"])
        self.assertTrue(candidate["eligible_for_authorization"])
        self.assertTrue(
            np.allclose(candidate["control_origin_from_tool_m"], [0.32, 0.0, 0.0])
        )
        self.assertTrue(
            np.allclose(
                candidate["control_rotation_matrix_from_tool"],
                [[1.0, 0.0, 0.0], [0.0, -1.0, 0.0], [0.0, 0.0, -1.0]],
            )
        )

    def test_low_confidence_rejects_candidate(self) -> None:
        candidate = build_control_frame_candidate(
            landmarks_target_m={
                "tip": [0.2, 0.0, 0.0],
                "heel": [0.4, 0.0, 0.0],
                "spine": [0.2, 0.0, 0.04],
            },
            target_from_tool=np.eye(4),
            geometry={
                "axis_start_role": "tip",
                "axis_end_role": "heel",
                "plane_role": "spine",
                "minimum_landmark_confidence": 0.8,
            },
            landmark_confidences={"tip": 0.4, "heel": 0.95, "spine": 0.91},
        )

        self.assertEqual(candidate["status"], "REJECTED_OBSERVATION")
        self.assertFalse(candidate["eligible_for_authorization"])
        self.assertIn("tip", candidate["quality_reasons"][0])

    def test_reflective_landmarks_use_closest_neighbor_with_provenance(self) -> None:
        depth = np.full((11, 11), np.nan)
        depth[2, 2] = 1.0
        depth[2, 8] = 1.0
        depth[8, 2] = 1.0
        depth[2, 3] = 0.8
        depth[3, 8] = 0.9
        depth[8, 3] = 0.95

        candidate = register_tool_to_control_frame_candidate(
            vlm_result={
                "backend_id": "vlm.test",
                "model": "test-model",
                "request_id": "request-1",
                "landmarks": [
                    {
                        "role": "tip",
                        "pixel_yx": [2, 2],
                        "confidence": 0.95,
                        "depth_policy": "CLOSEST_TO_CAMERA",
                    },
                    {
                        "role": "heel",
                        "pixel_yx": [2, 8],
                        "confidence": 0.95,
                        "depth_policy": "CLOSEST_TO_CAMERA",
                    },
                    {
                        "role": "spine",
                        "pixel_yx": [8, 2],
                        "confidence": 0.95,
                        "depth_policy": "CLOSEST_TO_CAMERA",
                    },
                ],
            },
            rgb_grid=(11, 11),
            registered_depth_m=depth,
            registered_depth_grid=(11, 11),
            intrinsics={"fx": 100.0, "fy": 100.0, "cx": 5.0, "cy": 5.0},
            target_from_camera=np.eye(4),
            target_from_tool=np.eye(4),
            observed_at_us=456,
            source_frame="camera",
            target_frame="workcell",
            calibration_revision="cal-2",
            route_provenance={"route_id": "generic", "boot_id": "boot-2"},
            geometry={
                "axis_start_role": "tip",
                "axis_end_role": "heel",
                "plane_role": "spine",
                "minimum_landmark_confidence": 0.8,
            },
            search_radius_px=1,
        )

        self.assertFalse(candidate["motion_usable"])
        self.assertEqual(candidate["observed_at_us"], 456)
        self.assertEqual(candidate["vlm_provenance"]["model"], "test-model")
        self.assertEqual(candidate["data_route"]["boot_id"], "boot-2")
        self.assertEqual(
            candidate["registered_landmarks"]["tip"]["depth_selection"]["policy"],
            "CLOSEST_TO_CAMERA",
        )
        self.assertAlmostEqual(
            candidate["registered_landmarks"]["tip"]["depth_selection"]["depth_m"],
            0.8,
        )


if __name__ == "__main__":
    unittest.main()
