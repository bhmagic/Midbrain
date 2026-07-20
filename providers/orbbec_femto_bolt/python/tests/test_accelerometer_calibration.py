from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from orbbec_femto_provider.accelerometer_calibration import (
    POSE_ORDER,
    CaptureSummary,
    build_custom_calibration_document,
    solve_six_position_calibration,
)
from orbbec_femto_provider.device_calibration import (
    load_or_create_accelerometer_calibration,
    write_accelerometer_calibration_document,
)


class AccelerometerCalibrationTests(unittest.TestCase):
    def test_six_position_solver_recovers_known_affine_model(self) -> None:
        gravity = 9.80665
        scale = np.asarray([1.018, 0.987, 1.011], dtype=np.float64)
        offset = np.asarray([0.11, -0.07, 0.045], dtype=np.float64)
        directions = np.asarray(
            [
                [1.0, 0.04, -0.03],
                [-1.0, -0.02, 0.05],
                [0.03, 1.0, 0.04],
                [-0.05, -1.0, -0.02],
                [0.04, -0.03, 1.0],
                [-0.02, 0.05, -1.0],
            ],
            dtype=np.float64,
        )
        directions /= np.linalg.norm(directions, axis=1, keepdims=True)
        corrected = directions * gravity
        raw = (corrected - offset) / scale

        captures = {
            pose: CaptureSummary(
                pose=pose,
                sample_count=200,
                mean_m_s2=tuple(float(value) for value in raw[index]),
                std_m_s2=(0.005, 0.005, 0.005),
                mean_temperature_c=25.0,
                duration_s=2.0,
                first_frame_number=index * 200,
                last_frame_number=index * 200 + 199,
            )
            for index, pose in enumerate(POSE_ORDER)
        }

        solution = solve_six_position_calibration(captures)
        np.testing.assert_allclose(solution.scale, scale, atol=1.0e-7)
        np.testing.assert_allclose(solution.offset, offset, atol=1.0e-7)
        self.assertLess(solution.rms_residual_m_s2, 1.0e-9)

    def test_custom_document_is_written_and_reloaded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current = load_or_create_accelerometer_calibration(
                root,
                manufacturer="Orbbec",
                model="Femto Bolt",
                serial_number="TEST123",
                firmware_version="test",
            )
            captures = {
                pose: CaptureSummary(
                    pose=pose,
                    sample_count=100,
                    mean_m_s2=values,
                    std_m_s2=(0.01, 0.01, 0.01),
                    mean_temperature_c=24.0,
                    duration_s=2.0,
                    first_frame_number=index * 100,
                    last_frame_number=index * 100 + 99,
                )
                for index, (pose, values) in enumerate(
                    zip(
                        POSE_ORDER,
                        (
                            (9.7, 0.1, -0.1),
                            (-9.9, 0.1, -0.1),
                            (0.1, 9.8, -0.1),
                            (0.1, -9.8, -0.1),
                            (0.1, 0.1, 9.75),
                            (0.1, 0.1, -9.85),
                        ),
                    )
                )
            }
            solution = solve_six_position_calibration(captures)
            document = build_custom_calibration_document(
                current.document,
                captures,
                solution,
            )
            written = write_accelerometer_calibration_document(
                current.path,
                document,
                expected_device_id=current.canonical_device_id,
            )
            self.assertEqual(written.status, "CUSTOM_CALIBRATED")
            self.assertTrue(current.path.exists())
            backups = list(current.path.parent.glob("imu-accelerometer.before-*.json"))
            self.assertEqual(len(backups), 1)


if __name__ == "__main__":
    unittest.main()
