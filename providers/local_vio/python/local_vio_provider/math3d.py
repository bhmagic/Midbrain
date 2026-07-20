from __future__ import annotations

import math
from typing import Iterable

import numpy as np


def normalize_quaternion_xyzw(value: Iterable[float]) -> np.ndarray:
    quaternion = np.asarray(tuple(value), dtype=np.float64)
    if quaternion.shape != (4,):
        raise ValueError("quaternion must contain four values")
    norm = float(np.linalg.norm(quaternion))
    if norm <= 1e-12:
        return np.array([0.0, 0.0, 0.0, 1.0], dtype=np.float64)
    return quaternion / norm


def quaternion_xyzw_to_matrix(value: Iterable[float]) -> np.ndarray:
    x, y, z, w = normalize_quaternion_xyzw(value)
    return np.array(
        [
            [1.0 - 2.0 * (y * y + z * z), 2.0 * (x * y - z * w), 2.0 * (x * z + y * w)],
            [2.0 * (x * y + z * w), 1.0 - 2.0 * (x * x + z * z), 2.0 * (y * z - x * w)],
            [2.0 * (x * z - y * w), 2.0 * (y * z + x * w), 1.0 - 2.0 * (x * x + y * y)],
        ],
        dtype=np.float64,
    )


def matrix_to_quaternion_xyzw(rotation: np.ndarray) -> np.ndarray:
    matrix = np.asarray(rotation, dtype=np.float64)
    if matrix.shape != (3, 3):
        raise ValueError("rotation matrix must be 3x3")
    trace = float(np.trace(matrix))
    if trace > 0.0:
        scale = math.sqrt(trace + 1.0) * 2.0
        w = 0.25 * scale
        x = (matrix[2, 1] - matrix[1, 2]) / scale
        y = (matrix[0, 2] - matrix[2, 0]) / scale
        z = (matrix[1, 0] - matrix[0, 1]) / scale
    elif matrix[0, 0] > matrix[1, 1] and matrix[0, 0] > matrix[2, 2]:
        scale = math.sqrt(1.0 + matrix[0, 0] - matrix[1, 1] - matrix[2, 2]) * 2.0
        w = (matrix[2, 1] - matrix[1, 2]) / scale
        x = 0.25 * scale
        y = (matrix[0, 1] + matrix[1, 0]) / scale
        z = (matrix[0, 2] + matrix[2, 0]) / scale
    elif matrix[1, 1] > matrix[2, 2]:
        scale = math.sqrt(1.0 + matrix[1, 1] - matrix[0, 0] - matrix[2, 2]) * 2.0
        w = (matrix[0, 2] - matrix[2, 0]) / scale
        x = (matrix[0, 1] + matrix[1, 0]) / scale
        y = 0.25 * scale
        z = (matrix[1, 2] + matrix[2, 1]) / scale
    else:
        scale = math.sqrt(1.0 + matrix[2, 2] - matrix[0, 0] - matrix[1, 1]) * 2.0
        w = (matrix[1, 0] - matrix[0, 1]) / scale
        x = (matrix[0, 2] + matrix[2, 0]) / scale
        y = (matrix[1, 2] + matrix[2, 1]) / scale
        z = 0.25 * scale
    return normalize_quaternion_xyzw([x, y, z, w])


def make_transform(rotation: np.ndarray, translation: Iterable[float]) -> np.ndarray:
    result = np.eye(4, dtype=np.float64)
    result[:3, :3] = np.asarray(rotation, dtype=np.float64)
    result[:3, 3] = np.asarray(tuple(translation), dtype=np.float64)
    return result


def invert_transform(transform: np.ndarray) -> np.ndarray:
    value = np.asarray(transform, dtype=np.float64)
    rotation = value[:3, :3]
    translation = value[:3, 3]
    result = np.eye(4, dtype=np.float64)
    result[:3, :3] = rotation.T
    result[:3, 3] = -(rotation.T @ translation)
    return result


def rotation_angle(rotation: np.ndarray) -> float:
    cosine = float((np.trace(rotation) - 1.0) * 0.5)
    return math.acos(max(-1.0, min(1.0, cosine)))


def gravity_aligned_world_from_camera(acceleration_camera: np.ndarray) -> np.ndarray:
    acceleration = np.asarray(acceleration_camera, dtype=np.float64)
    norm = float(np.linalg.norm(acceleration))
    if norm <= 1e-6:
        raise ValueError("acceleration magnitude is too small for gravity alignment")
    up_camera = acceleration / norm
    forward_hint = np.array([0.0, 0.0, 1.0], dtype=np.float64)
    forward_camera = forward_hint - up_camera * float(np.dot(forward_hint, up_camera))
    if float(np.linalg.norm(forward_camera)) < 1e-3:
        forward_hint = np.array([1.0, 0.0, 0.0], dtype=np.float64)
        forward_camera = forward_hint - up_camera * float(np.dot(forward_hint, up_camera))
    forward_camera /= np.linalg.norm(forward_camera)
    right_camera = np.cross(up_camera, forward_camera)
    right_camera /= np.linalg.norm(right_camera)
    forward_camera = np.cross(right_camera, up_camera)
    forward_camera /= np.linalg.norm(forward_camera)
    camera_from_world = np.column_stack((right_camera, up_camera, forward_camera))
    return camera_from_world.T
