from __future__ import annotations

import math
import unittest

import numpy as np

from local_vio_provider.prototype_backend import (
    STANDARD_GRAVITY_MPS2,
    PrototypeRgbdImuOdometry,
)
from local_vio_provider.math3d import (
    gravity_aligned_world_from_camera,
    gravity_leveled_world_from_camera_level,
)


class GravityStabilizationTests(unittest.TestCase):
    def test_tracking_quiet_imu_applies_small_rotation_only_correction(self) -> None:
        backend = self._backend_with_roll_error(12.0)
        self._add_stationary_window(backend, end_us=1_000_000)

        before = backend.world_from_camera.copy()
        backend.gravity_tracking_delay_s = 0.1
        backend._update_gravity_reference(correction_policy="TRACKING_STABILIZE")

        np.testing.assert_allclose(before[:3, 3], backend.world_from_camera[:3, 3])
        self.assertTrue(backend.last_gravity_correction_applied)
        self.assertEqual(backend.last_gravity_correction_mode, "TRACKING_LEVELING_ACTIVE")
        self.assertGreater(backend.last_gravity_tilt_error_rad or 0.0, math.radians(11.0))

    def test_tracking_motion_keeps_gravity_off(self) -> None:
        backend = self._backend_with_roll_error(12.0)
        self._add_window_with_gyro(backend, end_us=4_000_000, gyro_radps=0.04)

        before = backend.world_from_camera.copy()
        backend._update_gravity_reference(correction_policy="TRACKING_STABILIZE")

        np.testing.assert_allclose(before, backend.world_from_camera)
        self.assertFalse(backend.last_gravity_correction_applied)
        self.assertEqual(backend.last_gravity_adjustment_state, "OFF")
        self.assertEqual(backend.last_gravity_correction_mode, "GYRO_OR_ACCEL_NOT_STABLE")

    def test_degraded_stationary_gravity_reduces_roll_pitch_error(self) -> None:
        backend = self._backend_with_roll_error(12.0)
        backend.gravity_recovery_delay_s = 0.2
        backend.gravity_recovery_gain = 0.8
        backend.gravity_recovery_max_step_rad = 0.2
        self._add_stationary_window(backend, end_us=1_000_000, duration_s=1.2)

        initial_error = self._tilt_error_degrees(backend.world_from_camera[:3, :3])
        for _ in range(8):
            backend._update_gravity_reference(correction_policy="DEGRADED_RECOVERY")
        final_error = self._tilt_error_degrees(backend.world_from_camera[:3, :3])

        self.assertGreater(initial_error, 11.9)
        self.assertLess(final_error, 0.2)
        self.assertIn(
            backend.last_gravity_correction_mode,
            {"GRAVITY_ALIGNED", "DEGRADED_LEVELING_ACTIVE"},
        )

    def test_recovery_leaves_translation_unchanged(self) -> None:
        backend = self._backend_with_roll_error(30.0)
        backend.world_from_camera[:3, 3] = np.array([1.0, 2.0, 3.0])
        backend.gravity_recovery_delay_s = 0.2
        backend.gravity_recovery_gain = 1.0
        backend.gravity_recovery_max_step_rad = 1.0
        self._add_stationary_window(backend, end_us=2_000_000, duration_s=1.2)

        before_translation = backend.world_from_camera[:3, 3].copy()
        backend._update_gravity_reference(correction_policy="DEGRADED_RECOVERY")

        self.assertTrue(backend.last_gravity_correction_applied)
        np.testing.assert_allclose(before_translation, backend.world_from_camera[:3, 3])

    def test_gravity_recovery_uses_imu_time_domain(self) -> None:
        backend = self._backend_with_roll_error(35.0)
        backend.gravity_recovery_delay_s = 0.4
        backend.gravity_recovery_gain = 0.8
        backend.gravity_recovery_max_step_rad = 0.2
        self._add_stationary_window(backend, end_us=2_000_000, duration_s=1.6)

        initial_error = self._tilt_error_degrees(backend.world_from_camera[:3, :3])
        for _ in range(12):
            backend._update_gravity_reference(correction_policy="DEGRADED_RECOVERY")
        final_error = self._tilt_error_degrees(backend.world_from_camera[:3, :3])

        self.assertGreater(initial_error, 34.0)
        self.assertLess(final_error, 0.5)
        self.assertGreaterEqual(backend.last_gravity_stationary_duration_s, 0.9)

    def test_gravity_recovery_preserves_horizontal_heading(self) -> None:
        backend = PrototypeRgbdImuOdometry(
            gravity_recovery_delay_s=0.2,
            gravity_recovery_gain=1.0,
            gravity_recovery_max_step_rad=1.0,
        )
        backend.configure(np.eye(3), np.eye(4))
        backend.initialized = True
        yaw = math.radians(37.0)
        roll = math.radians(10.0)
        yaw_rotation = np.array(
            [
                [math.cos(yaw), -math.sin(yaw), 0.0],
                [math.sin(yaw), math.cos(yaw), 0.0],
                [0.0, 0.0, 1.0],
            ],
            dtype=np.float64,
        )
        roll_rotation = np.array(
            [
                [1.0, 0.0, 0.0],
                [0.0, math.cos(roll), -math.sin(roll)],
                [0.0, math.sin(roll), math.cos(roll)],
            ],
            dtype=np.float64,
        )
        level_rotation = gravity_aligned_world_from_camera(
            np.array([0.0, -1.0, 0.0])
        )
        backend.world_from_camera[:3, :3] = (
            yaw_rotation @ roll_rotation @ level_rotation
        )
        self._add_stationary_window(backend, end_us=1_000_000, duration_s=1.2)

        before_heading = self._heading_degrees(backend.world_from_camera[:3, :3])
        backend._update_gravity_reference(correction_policy="DEGRADED_RECOVERY")
        after_heading = self._heading_degrees(backend.world_from_camera[:3, :3])

        self.assertAlmostEqual(before_heading, after_heading, places=6)

    def test_slow_pan_is_not_classified_as_stationary(self) -> None:
        backend = self._backend_with_roll_error(8.0)
        end_us = 1_000_000
        for index in range(60):
            timestamp_us = end_us - 1_180_000 + index * 20_000
            backend.add_accelerometer(
                timestamp_us,
                np.array([0.0, -STANDARD_GRAVITY_MPS2, 0.0]),
                motion_inhibited=False,
            )
            backend.add_gyroscope(timestamp_us, np.array([0.0, 0.15, 0.0]))

        before = backend.world_from_camera.copy()
        backend._update_gravity_reference(correction_policy="DEGRADED_RECOVERY")

        np.testing.assert_allclose(before, backend.world_from_camera)
        self.assertFalse(backend.last_gravity_correction_applied)
        self.assertEqual(backend.last_gravity_correction_mode, "GYRO_OR_ACCEL_NOT_STABLE")
        self.assertEqual(backend.last_gravity_adjustment_state, "OFF")

    def test_adaptive_gyro_gate_accepts_quiet_residual_and_rejects_rotation(self) -> None:
        accepted = self._backend_with_roll_error(8.0)
        accepted.gravity_gyro_limit_radps = 0.012
        accepted.gravity_gyro_effective_limit_radps = 0.012
        self._add_window_with_gyro(accepted, end_us=1_000_000, gyro_radps=0.009)
        self.assertIsNotNone(accepted._stationary_gravity_observation())

        rejected = self._backend_with_roll_error(8.0)
        rejected.gravity_gyro_limit_radps = 0.012
        rejected.gravity_gyro_effective_limit_radps = 0.012
        self._add_window_with_gyro(rejected, end_us=1_000_000, gyro_radps=0.016)
        self.assertIsNone(rejected._stationary_gravity_observation())

    def test_stationary_gyro_baseline_subtracts_zero_rate_bias(self) -> None:
        backend = self._backend_with_roll_error(8.0)
        bias = np.array([0.006, -0.004, 0.005])
        for index in range(80):
            timestamp_us = index * 20_000
            backend.add_gyroscope(timestamp_us, bias + np.array([0.0002, -0.0001, 0.0001]))
        backend._estimate_stationary_gyro_baseline()
        np.testing.assert_allclose(backend.gyro_bias_imu_radps, bias + np.array([0.0002, -0.0001, 0.0001]))
        self.assertGreaterEqual(backend.gravity_gyro_effective_limit_radps, 0.008)
        self.assertLessEqual(backend.gravity_gyro_effective_limit_radps, 0.03)

    def test_dynamic_acceleration_is_not_used_for_recovery(self) -> None:
        backend = self._backend_with_roll_error(8.0)
        end_us = 1_000_000
        for index in range(30):
            timestamp_us = end_us - 580_000 + index * 20_000
            backend.add_accelerometer(
                timestamp_us,
                np.array([2.0, -STANDARD_GRAVITY_MPS2, 0.0]),
                motion_inhibited=False,
            )
            backend.add_gyroscope(timestamp_us, np.array([0.0, 1.0, 0.0]))

        before = backend.world_from_camera.copy()
        backend._update_gravity_reference(correction_policy="DEGRADED_RECOVERY")

        np.testing.assert_allclose(before, backend.world_from_camera)
        self.assertFalse(backend.last_gravity_correction_applied)

    @staticmethod
    def _backend_with_roll_error(degrees: float) -> PrototypeRgbdImuOdometry:
        backend = PrototypeRgbdImuOdometry()
        backend.configure(np.eye(3), np.eye(4))
        backend.initialized = True
        angle = math.radians(degrees)
        error_world = np.array(
            [
                [1.0, 0.0, 0.0],
                [0.0, math.cos(angle), -math.sin(angle)],
                [0.0, math.sin(angle), math.cos(angle)],
            ],
            dtype=np.float64,
        )
        backend.world_from_camera[:3, :3] = (
            error_world
            @ gravity_aligned_world_from_camera(
                np.array([0.0, -1.0, 0.0])
            )
        )
        return backend

    @staticmethod
    def _add_stationary_window(
        backend: PrototypeRgbdImuOdometry,
        *,
        end_us: int,
        duration_s: float = 1.2,
    ) -> None:
        sample_count = int(duration_s / 0.02) + 1
        start_us = end_us - int(duration_s * 1_000_000)
        for index in range(sample_count):
            timestamp_us = start_us + index * 20_000
            backend.add_accelerometer(
                timestamp_us,
                np.array([0.0, -STANDARD_GRAVITY_MPS2, 0.0]),
                motion_inhibited=False,
            )
            backend.add_gyroscope(timestamp_us, np.zeros(3))

    @staticmethod
    def _add_window_with_gyro(
        backend: PrototypeRgbdImuOdometry,
        *,
        end_us: int,
        gyro_radps: float,
    ) -> None:
        start_us = end_us - 1_200_000
        for index in range(61):
            timestamp_us = start_us + index * 20_000
            backend.add_accelerometer(
                timestamp_us,
                np.array([0.0, -STANDARD_GRAVITY_MPS2, 0.0]),
                motion_inhibited=False,
            )
            backend.add_gyroscope(
                timestamp_us,
                np.array([0.0, gyro_radps, 0.0]),
            )

    @staticmethod
    def _heading_degrees(rotation: np.ndarray) -> float:
        forward = rotation @ np.array([0.0, 0.0, 1.0])
        return math.degrees(math.atan2(forward[1], forward[0]))

    @staticmethod
    def _tilt_error_degrees(rotation: np.ndarray) -> float:
        observed_up = rotation @ np.array([0.0, -1.0, 0.0])
        cosine = float(
            np.clip(
                observed_up @ np.array([0.0, 0.0, 1.0]),
                -1.0,
                1.0,
            )
        )
        return math.degrees(math.acos(cosine))

    def test_initial_optical_frame_maps_forward_left_up_to_world_xyz(
        self,
    ) -> None:
        rotation = gravity_aligned_world_from_camera(
            np.array([0.0, -1.0, 0.0])
        )
        np.testing.assert_allclose(
            rotation @ np.array([0.0, 0.0, 1.0]),
            [1.0, 0.0, 0.0],
            atol=1e-9,
        )
        np.testing.assert_allclose(
            rotation @ np.array([-1.0, 0.0, 0.0]),
            [0.0, 1.0, 0.0],
            atol=1e-9,
        )
        np.testing.assert_allclose(
            rotation @ np.array([0.0, -1.0, 0.0]),
            [0.0, 0.0, 1.0],
            atol=1e-9,
        )

    def test_camera_level_preserves_origin_and_uses_z_up(self) -> None:
        world_from_camera = np.eye(4)
        world_from_camera[:3, :3] = (
            gravity_aligned_world_from_camera(
                np.array([0.0, -1.0, 0.0])
            )
        )
        world_from_camera[:3, 3] = [0.4, -0.2, 1.1]
        world_from_level = gravity_leveled_world_from_camera_level(
            world_from_camera
        )
        np.testing.assert_allclose(
            world_from_level[:3, 3],
            [0.4, -0.2, 1.1],
        )
        np.testing.assert_allclose(
            world_from_level[:3, :3],
            np.eye(3),
            atol=1e-9,
        )

    @staticmethod
    def _rotation_angle(rotation: np.ndarray) -> float:
        cosine = float(np.clip((np.trace(rotation) - 1.0) * 0.5, -1.0, 1.0))
        return math.acos(cosine)


if __name__ == "__main__":
    unittest.main()
