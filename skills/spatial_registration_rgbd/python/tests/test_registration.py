from __future__ import annotations

import unittest

import numpy as np

from spatial_registration_rgbd import (
    map_pixel_between_grids,
    register_rgbd_point,
    select_depth_sample,
)


class SpatialRegistrationTests(unittest.TestCase):
    def test_independent_rgb_and_registered_depth_grids_are_supported(self) -> None:
        mapped = map_pixel_between_grids(
            (539.5, 959.5),
            (1080, 1920),
            (576, 640),
        )

        self.assertAlmostEqual(mapped[0], 287.5)
        self.assertAlmostEqual(mapped[1], 319.5)

    def test_closest_depth_policy_can_use_neighbor_for_reflective_target(self) -> None:
        depth = np.full((9, 9), np.nan)
        depth[4, 3] = 0.82
        depth[4, 5] = 0.51
        depth[5, 4] = 0.74

        selection = select_depth_sample(
            depth,
            (4, 4),
            search_radius_px=2,
            policy="CLOSEST_TO_CAMERA",
        )

        self.assertEqual(selection.pixel_yx, (4, 5))
        self.assertAlmostEqual(selection.depth_m, 0.51)
        self.assertEqual(selection.requested_pixel_yx, (4, 4))

    def test_valid_boundary_excludes_misaligned_channel_padding(self) -> None:
        depth = np.ones((8, 8))

        with self.assertRaisesRegex(RuntimeError, "no valid nearby depth"):
            select_depth_sample(
                depth,
                (1, 1),
                search_radius_px=0,
                valid_region={"x": 2, "y": 2, "width": 4, "height": 4},
            )

    def test_registration_retains_transform_calibration_and_route_provenance(self) -> None:
        depth = np.full((4, 4), 1.0)
        target_from_camera = np.eye(4)
        target_from_camera[:3, 3] = [1.0, 2.0, 3.0]

        result = register_rgbd_point(
            rgb_pixel_yx=(1.5, 1.5),
            rgb_grid=(8, 8),
            registered_depth_m=depth,
            registered_depth_grid=(4, 4),
            intrinsics={"fx": 4.0, "fy": 4.0, "cx": 1.5, "cy": 1.5},
            target_from_camera=target_from_camera,
            observed_at_us=123,
            source_frame="camera",
            target_frame="world",
            calibration_revision="cal-1",
            route_provenance={"route_id": "generic", "boot_id": "boot-1"},
        )

        self.assertEqual(result["observed_at_us"], 123)
        self.assertEqual(result["calibration_revision"], "cal-1")
        self.assertEqual(result["data_route"]["boot_id"], "boot-1")
        self.assertEqual(
            result["camera_system_point_m"],
            {
                "camera_system_x": -0.375,
                "camera_system_y": -0.375,
                "camera_system_z": 1.0,
            },
        )
        self.assertTrue(
            np.allclose(result["target_point_m"], [0.625, 1.625, 4.0])
        )


if __name__ == "__main__":
    unittest.main()
