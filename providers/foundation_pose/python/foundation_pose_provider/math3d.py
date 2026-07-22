"""Small rigid-transform helpers used by the FoundationPose Provider."""

from __future__ import annotations

import math
from typing import Iterable

import numpy as np


def as_transform(value: object, *, field_name: str = "transform") -> np.ndarray:
    """Return a validated 4x4 homogeneous transform."""
    matrix = np.asarray(value, dtype=np.float64)
    if matrix.size != 16:
        raise ValueError(f"{field_name} must contain 16 numeric values")
    matrix = matrix.reshape(4, 4)
    if not np.all(np.isfinite(matrix)):
        raise ValueError(f"{field_name} contains non-finite values")
    if not np.allclose(matrix[3], [0.0, 0.0, 0.0, 1.0], atol=1e-6):
        raise ValueError(f"{field_name} must be a homogeneous 4x4 transform")
    rotation = matrix[:3, :3]
    if not np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-3):
        raise ValueError(f"{field_name} rotation is not orthonormal")
    if np.linalg.det(rotation) < 0.0:
        raise ValueError(f"{field_name} rotation must be right-handed")
    return matrix


def matrix_to_quaternion_xyzw(matrix: np.ndarray) -> list[float]:
    """Convert a 3x3 or 4x4 rotation matrix to an XYZW quaternion."""
    rotation = np.asarray(matrix, dtype=np.float64)[:3, :3]
    trace = float(np.trace(rotation))
    if trace > 0.0:
        scale = math.sqrt(trace + 1.0) * 2.0
        x = (rotation[2, 1] - rotation[1, 2]) / scale
        y = (rotation[0, 2] - rotation[2, 0]) / scale
        z = (rotation[1, 0] - rotation[0, 1]) / scale
        w = 0.25 * scale
    elif rotation[0, 0] > rotation[1, 1] and rotation[0, 0] > rotation[2, 2]:
        scale = math.sqrt(1.0 + rotation[0, 0] - rotation[1, 1] - rotation[2, 2]) * 2.0
        x = 0.25 * scale
        y = (rotation[0, 1] + rotation[1, 0]) / scale
        z = (rotation[0, 2] + rotation[2, 0]) / scale
        w = (rotation[2, 1] - rotation[1, 2]) / scale
    elif rotation[1, 1] > rotation[2, 2]:
        scale = math.sqrt(1.0 + rotation[1, 1] - rotation[0, 0] - rotation[2, 2]) * 2.0
        x = (rotation[0, 1] + rotation[1, 0]) / scale
        y = 0.25 * scale
        z = (rotation[1, 2] + rotation[2, 1]) / scale
        w = (rotation[0, 2] - rotation[2, 0]) / scale
    else:
        scale = math.sqrt(1.0 + rotation[2, 2] - rotation[0, 0] - rotation[1, 1]) * 2.0
        x = (rotation[0, 2] + rotation[2, 0]) / scale
        y = (rotation[1, 2] + rotation[2, 1]) / scale
        z = 0.25 * scale
        w = (rotation[1, 0] - rotation[0, 1]) / scale
    quaternion = np.asarray([x, y, z, w], dtype=np.float64)
    norm = float(np.linalg.norm(quaternion))
    if norm <= 1e-12:
        raise ValueError("rotation produced a zero-length quaternion")
    quaternion /= norm
    return quaternion.tolist()


def transform_payload(matrix: np.ndarray) -> dict[str, object]:
    """Return translation, quaternion, and row-major matrix payload fields."""
    transform = as_transform(matrix)
    return {
        "translation_m": transform[:3, 3].astype(float).tolist(),
        "quaternion_xyzw": matrix_to_quaternion_xyzw(transform),
        "matrix_4x4_row_major": transform.astype(float).reshape(-1).tolist(),
    }


def diagonal_covariance(
    translation_sigma_m: Iterable[float], rotation_sigma_rad: Iterable[float]
) -> list[float]:
    """Return a row-major 6x6 diagonal covariance matrix."""
    values = [float(value) for value in translation_sigma_m] + [
        float(value) for value in rotation_sigma_rad
    ]
    if len(values) != 6 or any(value < 0.0 for value in values):
        raise ValueError("covariance sigmas must contain six non-negative values")
    covariance = np.zeros((6, 6), dtype=np.float64)
    for index, sigma in enumerate(values):
        covariance[index, index] = sigma * sigma
    return covariance.reshape(-1).tolist()
