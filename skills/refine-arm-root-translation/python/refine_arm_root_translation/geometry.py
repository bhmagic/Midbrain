from __future__ import annotations

from typing import Any

import numpy as np


def finite_vector(value: Any, name: str) -> np.ndarray:
    vector = np.asarray(value, dtype=np.float64)
    if vector.shape != (3,) or not np.all(np.isfinite(vector)):
        raise ValueError(f"{name} must be a finite 3-vector")
    return vector


def rigid_transform(value: Any, name: str) -> np.ndarray:
    matrix = np.asarray(value, dtype=np.float64)
    if matrix.shape != (4, 4) or not np.all(np.isfinite(matrix)):
        raise ValueError(f"{name} must be a finite 4x4 transform")
    if not np.allclose(matrix[3], [0.0, 0.0, 0.0, 1.0], atol=1e-9):
        raise ValueError(f"{name} must have a rigid homogeneous last row")
    rotation = matrix[:3, :3]
    if not np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-6):
        raise ValueError(f"{name} rotation must be orthonormal")
    if not np.isclose(np.linalg.det(rotation), 1.0, atol=1e-6):
        raise ValueError(f"{name} rotation must be right-handed")
    return matrix


def apply_transform(transform: Any, point: Any) -> np.ndarray:
    matrix = rigid_transform(transform, "transform")
    vector = finite_vector(point, "point")
    return matrix[:3, :3] @ vector + matrix[:3, 3]


def transform_from_pose(value: Any, name: str = "pose") -> np.ndarray:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    translation = finite_vector(value.get("translation_m"), f"{name}.translation_m")
    quaternion = np.asarray(value.get("rotation_xyzw"), dtype=np.float64)
    if quaternion.shape != (4,) or not np.all(np.isfinite(quaternion)):
        raise ValueError(f"{name}.rotation_xyzw must be a finite quaternion")
    norm = float(np.linalg.norm(quaternion))
    if not np.isclose(norm, 1.0, atol=1e-6):
        raise ValueError(f"{name}.rotation_xyzw must be normalized")
    x, y, z, w = quaternion / norm
    rotation = np.asarray(
        [
            [1.0 - 2.0 * (y * y + z * z), 2.0 * (x * y - z * w), 2.0 * (x * z + y * w)],
            [2.0 * (x * y + z * w), 1.0 - 2.0 * (x * x + z * z), 2.0 * (y * z - x * w)],
            [2.0 * (x * z - y * w), 2.0 * (y * z + x * w), 1.0 - 2.0 * (x * x + y * y)],
        ],
        dtype=np.float64,
    )
    result = np.eye(4, dtype=np.float64)
    result[:3, :3] = rotation
    result[:3, 3] = translation
    return rigid_transform(result, name)


def camera_intrinsics(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        raise ValueError("intrinsics must be an object")
    result: dict[str, float] = {}
    for field in ("fx", "fy", "cx", "cy"):
        try:
            number = float(value[field])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"intrinsics.{field} must be finite") from error
        if not np.isfinite(number):
            raise ValueError(f"intrinsics.{field} must be finite")
        result[field] = number
    if result["fx"] <= 0.0 or result["fy"] <= 0.0:
        raise ValueError("intrinsics focal lengths must be positive")
    return result


def deproject_registered_depth_pixel(
    pixel_yx: tuple[int, int] | list[int],
    depth_m: float,
    intrinsics: Any,
) -> np.ndarray:
    calibration = camera_intrinsics(intrinsics)
    if (
        not isinstance(pixel_yx, (tuple, list))
        or len(pixel_yx) != 2
    ):
        raise ValueError("pixel_yx must contain y and x")
    y, x = float(pixel_yx[0]), float(pixel_yx[1])
    depth = float(depth_m)
    if not np.isfinite(depth) or depth <= 0.0:
        raise ValueError("depth_m must be positive and finite")
    return np.asarray(
        [
            (x - calibration["cx"]) * depth / calibration["fx"],
            (y - calibration["cy"]) * depth / calibration["fy"],
            depth,
        ],
        dtype=np.float64,
    )


def project_camera_point(
    point_camera_m: Any,
    intrinsics: Any,
) -> tuple[float, float]:
    point = finite_vector(point_camera_m, "point_camera_m")
    if point[2] <= 0.0:
        raise ValueError("point_camera_m must be in front of the camera")
    calibration = camera_intrinsics(intrinsics)
    x = calibration["fx"] * point[0] / point[2] + calibration["cx"]
    y = calibration["fy"] * point[1] / point[2] + calibration["cy"]
    return float(y), float(x)
