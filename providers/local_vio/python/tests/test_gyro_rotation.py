from __future__ import annotations

import math
import types
import unittest

import numpy as np

from local_vio_provider.prototype_backend import (
    PrototypeRgbdImuOdometry,
    _FeatureSet,
    _FrameState,
)


class GyroRotationTests(unittest.TestCase):
    def test_integrated_gyro_rotation_has_expected_angle(self) -> None:
        backend = PrototypeRgbdImuOdometry()
        backend.configure(np.eye(3), np.eye(4))
        rate = math.radians(90.0)
        for index in range(51):
            backend.add_gyroscope(index * 20_000, np.array([0.0, rate, 0.0]))

        rotation, sample_count = backend._integrated_gyro_rotation(0, 1_000_000)

        self.assertIsNotNone(rotation)
        self.assertGreaterEqual(sample_count, 50)
        angle = math.acos(float(np.clip((np.trace(rotation) - 1.0) * 0.5, -1.0, 1.0)))
        self.assertAlmostEqual(angle, math.pi / 2.0, places=3)

    def test_gyro_rotation_respects_imu_to_camera_extrinsics(self) -> None:
        backend = PrototypeRgbdImuOdometry()
        color_from_imu = np.eye(4)
        # Rotate IMU X onto camera Z.
        color_from_imu[:3, :3] = np.array(
            [
                [0.0, 0.0, 1.0],
                [0.0, 1.0, 0.0],
                [-1.0, 0.0, 0.0],
            ]
        )
        backend.configure(np.eye(3), color_from_imu)
        rate = 0.5
        for index in range(11):
            backend.add_gyroscope(index * 100_000, np.array([rate, 0.0, 0.0]))

        rotation, _ = backend._integrated_gyro_rotation(0, 1_000_000)

        self.assertIsNotNone(rotation)
        # Camera Z-axis rotation leaves camera Z unchanged.
        transformed_z = rotation @ np.array([0.0, 0.0, 1.0])
        np.testing.assert_allclose(transformed_z, np.array([0.0, 0.0, 1.0]), atol=1e-6)

    def test_full_rotation_disagreement_detects_wrong_axis(self) -> None:
        backend = PrototypeRgbdImuOdometry()
        angle = 0.4
        visual = np.array(
            [
                [math.cos(angle), -math.sin(angle), 0.0],
                [math.sin(angle), math.cos(angle), 0.0],
                [0.0, 0.0, 1.0],
            ]
        )
        gyro = np.array(
            [
                [math.cos(angle), 0.0, math.sin(angle)],
                [0.0, 1.0, 0.0],
                [-math.sin(angle), 0.0, math.cos(angle)],
            ]
        )
        disagreement = math.acos(
            float(np.clip((np.trace(visual @ gyro.T) - 1.0) * 0.5, -1.0, 1.0))
        )
        self.assertGreater(disagreement, backend._gyro_visual_disagreement_limit(angle))

    def test_wrong_visual_rotation_is_not_reported_as_tracking(self) -> None:
        backend = PrototypeRgbdImuOdometry()
        backend.configure(np.array([[500.0, 0.0, 32.0], [0.0, 500.0, 24.0], [0.0, 0.0, 1.0]]), np.eye(4))
        backend.initialized = True
        gray = np.zeros((48, 64), dtype=np.uint8)
        depth = np.ones((48, 64), dtype=np.float32) * 2.0
        backend.previous = _FrameState(
            0,
            gray,
            depth,
            {"RAW_BASELINE": _FeatureSet("RAW_BASELINE", [], None)},
            0.0,
        )
        for index in range(11):
            backend.add_gyroscope(index * 10_000, np.array([0.0, 4.0, 0.0]))

        wrong_rotation = np.array(
            [
                [math.cos(0.4), -math.sin(0.4), 0.0],
                [math.sin(0.4), math.cos(0.4), 0.0],
                [0.0, 0.0, 1.0],
            ]
        )
        wrong_step = np.eye(4)
        wrong_step[:3, :3] = wrong_rotation

        def fake_estimate(self, *_args, **_kwargs):
            return wrong_step, 120, 140, "RAW_BASELINE", "VISUAL_EPNP", 0.8

        backend._estimate_best_step = types.MethodType(fake_estimate, backend)
        result = backend.process(
            np.zeros((48, 64, 3), dtype=np.uint8),
            depth,
            100_000,
        )

        self.assertEqual(result.tracking_state, "DEGRADED")
        self.assertEqual(result.rotation_source, "GYRO_PROPAGATION")
        self.assertIsNotNone(result.rotation_disagreement_rad)



if __name__ == "__main__":
    unittest.main()
