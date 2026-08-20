from __future__ import annotations

import numpy as np

from locate_arm_base.math3d import matrix4, quaternion_xyzw, x_rotation, z_rotation


def test_profiled_quarter_turn_is_right_handed() -> None:
    rotation = z_rotation(90)
    np.testing.assert_allclose(rotation[:3, 0], [0.0, 1.0, 0.0], atol=1e-12)
    np.testing.assert_allclose(quaternion_xyzw(rotation[:3, :3]), [0.0, 0.0, 2**-0.5, 2**-0.5], atol=1e-12)
    matrix4(rotation)


def test_non_rigid_transform_is_rejected() -> None:
    invalid = np.eye(4)
    invalid[0, 0] = 2.0
    try:
        matrix4(invalid)
    except ValueError as exc:
        assert "orthonormal" in str(exc)
    else:
        raise AssertionError("non-rigid matrix was accepted")


def test_profiled_local_x_half_turn_flips_only_y_and_z() -> None:
    rotation = x_rotation(180)
    np.testing.assert_allclose(rotation[:3, :3], np.diag([1.0, -1.0, -1.0]), atol=1e-12)
    np.testing.assert_allclose(
        quaternion_xyzw(rotation[:3, :3]), [1.0, 0.0, 0.0, 0.0], atol=1e-12
    )
    matrix4(rotation)
