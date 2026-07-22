from __future__ import annotations

import unittest

import numpy as np

from foundation_pose_provider.math3d import (
    as_transform,
    diagonal_covariance,
    matrix_to_quaternion_xyzw,
    transform_payload,
)


class Math3dTests(unittest.TestCase):
    def test_identity_transform_payload(self) -> None:
        payload = transform_payload(np.eye(4))
        self.assertEqual(payload["translation_m"], [0.0, 0.0, 0.0])
        np.testing.assert_allclose(payload["quaternion_xyzw"], [0.0, 0.0, 0.0, 1.0])
        self.assertEqual(len(payload["matrix_4x4_row_major"]), 16)

    def test_invalid_transform_rejected(self) -> None:
        invalid = np.eye(4)
        invalid[3, 3] = 2.0
        with self.assertRaises(ValueError):
            as_transform(invalid)

    def test_rotation_quaternion(self) -> None:
        rotation = np.array(
            [
                [0.0, -1.0, 0.0],
                [1.0, 0.0, 0.0],
                [0.0, 0.0, 1.0],
            ]
        )
        quaternion = matrix_to_quaternion_xyzw(rotation)
        np.testing.assert_allclose(np.abs(quaternion), [0.0, 0.0, np.sqrt(0.5), np.sqrt(0.5)])

    def test_covariance_shape(self) -> None:
        covariance = diagonal_covariance((1.0, 2.0, 3.0), (4.0, 5.0, 6.0))
        self.assertEqual(len(covariance), 36)
        self.assertEqual(covariance[0], 1.0)
        self.assertEqual(covariance[7], 4.0)
        self.assertEqual(covariance[35], 36.0)


if __name__ == "__main__":
    unittest.main()
