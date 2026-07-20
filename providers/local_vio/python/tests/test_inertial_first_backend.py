from __future__ import annotations

import math
import unittest

import numpy as np

from local_vio_provider.inertial_first_backend import (
    InertialFirstRgbdVio,
    _VisualCandidate,
)
from local_vio_provider.prototype_backend import STANDARD_GRAVITY_MPS2


class InertialFirstBackendTests(unittest.TestCase):
    def make_backend(self) -> InertialFirstRgbdVio:
        backend = InertialFirstRgbdVio(
            gravity_samples=20,
            gravity_tracking_delay_s=0.1,
            visual_stale_timeout_s=0.2,
            feature_preprocess_mode="raw_baseline",
        )
        camera = np.array(
            [[500.0, 0.0, 32.0], [0.0, 500.0, 24.0], [0.0, 0.0, 1.0]],
            dtype=np.float64,
        )
        backend.configure(camera, np.eye(4))
        return backend

    def initialize(self, backend: InertialFirstRgbdVio, end_us: int = 400_000) -> None:
        for index in range(25):
            timestamp = index * 20_000
            backend.add_accelerometer(
                timestamp,
                np.array([0.0, STANDARD_GRAVITY_MPS2, 0.0]),
                motion_inhibited=True,
            )
            backend.add_gyroscope(timestamp, np.zeros(3))
        rgb = np.zeros((48, 64, 3), dtype=np.uint8)
        depth = np.ones((48, 64), dtype=np.float32) * 2.0
        result = backend.process(rgb, depth, end_us)
        self.assertEqual(result.tracking_state, "TRACKING")
        self.assertTrue(backend.initialized)


    def test_initialization_uses_common_imu_time_not_camera_time(self) -> None:
        backend = self.make_backend()
        for index in range(25):
            timestamp = 5_000_000 + index * 20_000
            backend.add_accelerometer(
                timestamp,
                np.array([0.0, STANDARD_GRAVITY_MPS2, 0.0]),
                motion_inhibited=True,
            )
            backend.add_gyroscope(timestamp, np.array([0.01, -0.02, 0.005]))
        rgb = np.zeros((48, 64, 3), dtype=np.uint8)
        depth = np.ones((48, 64), dtype=np.float32) * 2.0
        # Deliberately use an unrelated camera timestamp. Initialization must
        # select the stationary window entirely in the common IMU time domain.
        result = backend.process(rgb, depth, 90_000_000)
        self.assertEqual(result.tracking_state, "TRACKING")
        self.assertTrue(backend.initialized)
        self.assertIsNone(result.initialization_blocker)
        self.assertGreaterEqual(result.imu_gyroscope_history_count, 25)


    def test_default_80_sample_initialization_succeeds_at_50_hz(self) -> None:
        backend = InertialFirstRgbdVio(
            gravity_samples=80,
            feature_preprocess_mode="raw_baseline",
        )
        camera = np.array(
            [[500.0, 0.0, 32.0], [0.0, 500.0, 24.0], [0.0, 0.0, 1.0]],
            dtype=np.float64,
        )
        backend.configure(camera, np.eye(4))
        for index in range(100):
            timestamp = 10_000_000 + index * 20_000
            backend.add_accelerometer(
                timestamp,
                np.array([0.0, STANDARD_GRAVITY_MPS2, 0.0]),
                motion_inhibited=True,
            )
            backend.add_gyroscope(timestamp, np.zeros(3))
        rgb = np.zeros((48, 64, 3), dtype=np.uint8)
        depth = np.ones((48, 64), dtype=np.float32) * 2.0
        result = backend.process(rgb, depth, 11_980_000)
        self.assertEqual(result.tracking_state, "TRACKING")
        self.assertTrue(backend.initialized)
        self.assertIsNone(result.initialization_blocker)
        self.assertEqual(result.initialization_accelerometer_window_count, 80)
        self.assertEqual(result.initialization_gyroscope_window_count, 80)
        self.assertAlmostEqual(result.initialization_accelerometer_rate_hz or 0.0, 50.0, places=3)
        self.assertAlmostEqual(result.initialization_gyroscope_rate_hz or 0.0, 50.0, places=3)

    def test_stationary_imu_propagation_holds_pose(self) -> None:
        backend = self.make_backend()
        self.initialize(backend)
        for index in range(21, 71):
            timestamp = index * 20_000
            backend.add_accelerometer(
                timestamp,
                np.array([0.0, STANDARD_GRAVITY_MPS2, 0.0]),
                motion_inhibited=False,
            )
            backend.add_gyroscope(timestamp, np.zeros(3))
        predicted = backend.predict_latest()
        self.assertIsNotNone(predicted)
        assert predicted is not None
        np.testing.assert_allclose(predicted.world_from_camera[:3, 3], np.zeros(3), atol=2e-3)
        np.testing.assert_allclose(predicted.velocity_world_mps, np.zeros(3), atol=3e-3)

    def test_fast_yaw_is_propagated_from_gyro_without_visual_frames(self) -> None:
        backend = self.make_backend()
        self.initialize(backend)
        rate = math.pi / 2.0
        for index in range(21, 71):
            timestamp = index * 20_000
            backend.add_accelerometer(
                timestamp,
                np.array([0.0, STANDARD_GRAVITY_MPS2, 0.0]),
                motion_inhibited=False,
            )
            backend.add_gyroscope(timestamp, np.array([0.0, rate, 0.0]))
        predicted = backend.predict_latest()
        self.assertIsNotNone(predicted)
        assert predicted is not None
        forward = predicted.world_from_camera[:3, :3] @ np.array([0.0, 0.0, 1.0])
        yaw = math.atan2(forward[0], forward[2])
        self.assertGreater(abs(yaw), 1.35)
        self.assertLess(abs(yaw), 1.50)
        self.assertEqual(predicted.pose_update_mode, "IMU_FAST_PROPAGATION")

    def test_visual_pose_is_a_measurement_update_not_state_replacement(self) -> None:
        backend = self.make_backend()
        self.initialize(backend)
        assert backend.state is not None
        predicted = backend.state.position_world_m.copy()
        position_variance_before = float(backend.state.covariance[3, 3])
        measurement = np.eye(4)
        measurement[:3, 3] = np.array([0.08, 0.0, 0.0])
        candidate = _VisualCandidate(
            sensor="RGBD",
            world_from_imu_measurement=measurement,
            relative_current_from_previous=np.eye(4),
            inlier_count=150,
            match_count=170,
            feature_mode="RAW_BASELINE",
            solver_mode="VISUAL_EPNP",
            reprojection_rmse_px=0.7,
            raw_keypoint_count=1000,
            normalized_keypoint_count=0,
            luma_median=100.0,
            position_innovation_m=0.08,
            rotation_innovation_rad=0.0,
            normalized_score=100.0,
        )
        accepted = backend._apply_visual_measurement(candidate)
        self.assertTrue(accepted)
        correction = backend.state.position_world_m - predicted
        self.assertGreater(correction[0], 0.0)
        self.assertLess(correction[0], 0.08)
        self.assertLess(float(backend.state.covariance[3, 3]), position_variance_before)

    def test_healthy_rgb_is_preferred_over_ir(self) -> None:
        backend = self.make_backend()
        identity = np.eye(4)
        rgb = _VisualCandidate(
            "RGBD", identity, identity, 90, 100, "RAW_BASELINE", "VISUAL_EPNP", 1.0,
            900, 0, 80.0, 0.01, 0.01, 40.0,
        )
        ir = _VisualCandidate(
            "IR_DEPTH", identity, identity, 110, 125, "RAW_BASELINE", "VISUAL_EPNP", 0.8,
            1000, 0, 120.0, 0.01, 0.01, 50.0,
        )
        self.assertIs(backend._select_visual_candidate(rgb, ir), rgb)

    def test_ir_can_replace_weak_rgb(self) -> None:
        backend = self.make_backend()
        identity = np.eye(4)
        rgb = _VisualCandidate(
            "RGBD", identity, identity, 30, 45, "RAW_BASELINE", "VISUAL_EPNP", 2.5,
            350, 0, 25.0, 0.05, 0.05, 5.0,
        )
        ir = _VisualCandidate(
            "IR_DEPTH", identity, identity, 70, 90, "RAW_BASELINE", "VISUAL_EPNP", 1.0,
            800, 0, 110.0, 0.03, 0.03, 25.0,
        )
        self.assertIs(backend._select_visual_candidate(rgb, ir), ir)


if __name__ == "__main__":
    unittest.main()
