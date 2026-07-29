from __future__ import annotations

from typing import Any

import numpy as np
from spatial_registration_rgbd import deproject_pixel


def normalized_yx_to_pixel(point_yx: list[int] | tuple[int, int], shape: tuple[int, ...]) -> tuple[int, int]:
    height, width = shape[:2]
    y = int(round(float(point_yx[0]) * max(0, height - 1) / 1000.0))
    x = int(round(float(point_yx[1]) * max(0, width - 1) / 1000.0))
    return max(0, min(height - 1, y)), max(0, min(width - 1, x))


def quaternion_matrix(rotation_xyzw: list[float] | tuple[float, float, float, float]) -> np.ndarray:
    x, y, z, w = np.asarray(rotation_xyzw, dtype=np.float64)
    norm = float(np.linalg.norm([x, y, z, w]))
    if norm <= 1e-12:
        raise ValueError("rotation quaternion has zero norm")
    x, y, z, w = x / norm, y / norm, z / norm, w / norm
    return np.asarray(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ],
        dtype=np.float64,
    )


def matrix_quaternion_xyzw(rotation: np.ndarray) -> np.ndarray:
    matrix = np.asarray(rotation, dtype=np.float64)
    if matrix.shape != (3, 3) or not np.all(np.isfinite(matrix)):
        raise ValueError("rotation must be a finite 3x3 matrix")
    trace = float(np.trace(matrix))
    if trace > 0.0:
        scale = np.sqrt(trace + 1.0) * 2.0
        quaternion = np.asarray(
            [
                (matrix[2, 1] - matrix[1, 2]) / scale,
                (matrix[0, 2] - matrix[2, 0]) / scale,
                (matrix[1, 0] - matrix[0, 1]) / scale,
                0.25 * scale,
            ],
            dtype=np.float64,
        )
    else:
        diagonal = np.diag(matrix)
        index = int(np.argmax(diagonal))
        if index == 0:
            scale = np.sqrt(1.0 + matrix[0, 0] - matrix[1, 1] - matrix[2, 2]) * 2.0
            quaternion = np.asarray(
                [
                    0.25 * scale,
                    (matrix[0, 1] + matrix[1, 0]) / scale,
                    (matrix[0, 2] + matrix[2, 0]) / scale,
                    (matrix[2, 1] - matrix[1, 2]) / scale,
                ],
                dtype=np.float64,
            )
        elif index == 1:
            scale = np.sqrt(1.0 + matrix[1, 1] - matrix[0, 0] - matrix[2, 2]) * 2.0
            quaternion = np.asarray(
                [
                    (matrix[0, 1] + matrix[1, 0]) / scale,
                    0.25 * scale,
                    (matrix[1, 2] + matrix[2, 1]) / scale,
                    (matrix[0, 2] - matrix[2, 0]) / scale,
                ],
                dtype=np.float64,
            )
        else:
            scale = np.sqrt(1.0 + matrix[2, 2] - matrix[0, 0] - matrix[1, 1]) * 2.0
            quaternion = np.asarray(
                [
                    (matrix[0, 2] + matrix[2, 0]) / scale,
                    (matrix[1, 2] + matrix[2, 1]) / scale,
                    0.25 * scale,
                    (matrix[1, 0] - matrix[0, 1]) / scale,
                ],
                dtype=np.float64,
            )
    quaternion /= np.linalg.norm(quaternion)
    return quaternion


def matrix_rpy(rotation: np.ndarray) -> np.ndarray:
    """Return XYZ roll/pitch/yaw for an Rz(yaw) Ry(pitch) Rx(roll) matrix."""
    matrix = np.asarray(rotation, dtype=np.float64)
    if matrix.shape != (3, 3) or not np.all(np.isfinite(matrix)):
        raise ValueError("rotation must be a finite 3x3 matrix")
    pitch = float(np.arcsin(np.clip(-matrix[2, 0], -1.0, 1.0)))
    cosine_pitch = float(np.cos(pitch))
    if abs(cosine_pitch) > 1e-8:
        roll = float(np.arctan2(matrix[2, 1], matrix[2, 2]))
        yaw = float(np.arctan2(matrix[1, 0], matrix[0, 0]))
    else:
        roll = 0.0
        yaw = float(np.arctan2(-matrix[0, 1], matrix[1, 1]))
    return np.asarray([roll, pitch, yaw], dtype=np.float64)


def rotation_angle_rad(first: np.ndarray, second: np.ndarray) -> float:
    first_matrix = np.asarray(first, dtype=np.float64)
    second_matrix = np.asarray(second, dtype=np.float64)
    if first_matrix.shape != (3, 3) or second_matrix.shape != (3, 3):
        raise ValueError("rotation comparison requires two 3x3 matrices")
    relative = first_matrix @ second_matrix.T
    cosine = float(np.clip((np.trace(relative) - 1.0) * 0.5, -1.0, 1.0))
    return float(np.arccos(cosine))


def transform_matrix(translation_m: list[float], rotation_xyzw: list[float]) -> np.ndarray:
    output = np.eye(4, dtype=np.float64)
    output[:3, :3] = quaternion_matrix(rotation_xyzw)
    output[:3, 3] = np.asarray(translation_m, dtype=np.float64)
    return output


def transform_points(matrix: np.ndarray, points: np.ndarray) -> np.ndarray:
    values = np.asarray(points, dtype=np.float64)
    single = values.ndim == 1
    values = values.reshape(-1, 3)
    homogeneous = np.column_stack([values, np.ones(values.shape[0])])
    transformed = (np.asarray(matrix, dtype=np.float64) @ homogeneous.T).T[:, :3]
    return transformed[0] if single else transformed
