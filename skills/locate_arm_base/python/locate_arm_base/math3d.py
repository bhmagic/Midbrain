from __future__ import annotations

from collections.abc import Iterable
import math

import numpy as np


def matrix4(value: object, label: str = "transform") -> np.ndarray:
    matrix = np.asarray(value, dtype=np.float64)
    if matrix.shape != (4, 4) or not np.all(np.isfinite(matrix)):
        raise ValueError(f"{label} must be a finite 4x4 matrix")
    if not np.allclose(matrix[3], [0.0, 0.0, 0.0, 1.0], atol=1e-7):
        raise ValueError(f"{label} must be a homogeneous rigid transform")
    rotation = matrix[:3, :3]
    if not np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-5):
        raise ValueError(f"{label} rotation must be orthonormal")
    if not np.isclose(np.linalg.det(rotation), 1.0, atol=1e-5):
        raise ValueError(f"{label} rotation must be right-handed")
    return matrix


def z_rotation(degrees: float) -> np.ndarray:
    angle = math.radians(float(degrees))
    cosine, sine = math.cos(angle), math.sin(angle)
    result = np.eye(4, dtype=np.float64)
    result[:3, :3] = [[cosine, -sine, 0.0], [sine, cosine, 0.0], [0.0, 0.0, 1.0]]
    return result


def x_rotation(degrees: float) -> np.ndarray:
    angle = math.radians(float(degrees))
    cosine, sine = math.cos(angle), math.sin(angle)
    result = np.eye(4, dtype=np.float64)
    result[:3, :3] = [[1.0, 0.0, 0.0], [0.0, cosine, -sine], [0.0, sine, cosine]]
    return result


def quaternion_xyzw(rotation: np.ndarray) -> list[float]:
    matrix = np.asarray(rotation, dtype=np.float64)
    trace = float(np.trace(matrix))
    if trace > 0:
        scale = math.sqrt(trace + 1.0) * 2.0
        x = (matrix[2, 1] - matrix[1, 2]) / scale
        y = (matrix[0, 2] - matrix[2, 0]) / scale
        z = (matrix[1, 0] - matrix[0, 1]) / scale
        w = 0.25 * scale
    else:
        index = int(np.argmax(np.diag(matrix)))
        if index == 0:
            scale = math.sqrt(1.0 + matrix[0, 0] - matrix[1, 1] - matrix[2, 2]) * 2.0
            x, y, z, w = 0.25 * scale, (matrix[0, 1] + matrix[1, 0]) / scale, (matrix[0, 2] + matrix[2, 0]) / scale, (matrix[2, 1] - matrix[1, 2]) / scale
        elif index == 1:
            scale = math.sqrt(1.0 + matrix[1, 1] - matrix[0, 0] - matrix[2, 2]) * 2.0
            x, y, z, w = (matrix[0, 1] + matrix[1, 0]) / scale, 0.25 * scale, (matrix[1, 2] + matrix[2, 1]) / scale, (matrix[0, 2] - matrix[2, 0]) / scale
        else:
            scale = math.sqrt(1.0 + matrix[2, 2] - matrix[0, 0] - matrix[1, 1]) * 2.0
            x, y, z, w = (matrix[0, 2] + matrix[2, 0]) / scale, (matrix[1, 2] + matrix[2, 1]) / scale, 0.25 * scale, (matrix[1, 0] - matrix[0, 1]) / scale
    quaternion = np.asarray([x, y, z, w], dtype=np.float64)
    quaternion /= np.linalg.norm(quaternion)
    if quaternion[3] < 0:
        quaternion *= -1
    return quaternion.tolist()


def transform_from_translation_quaternion(
    translation_m: Iterable[float], rotation_xyzw: Iterable[float]
) -> np.ndarray:
    x, y, z, w = np.asarray(list(rotation_xyzw), dtype=np.float64)
    norm = math.sqrt(x * x + y * y + z * z + w * w)
    if not math.isfinite(norm) or norm < 1e-12:
        raise ValueError("rotation quaternion is invalid")
    x, y, z, w = x / norm, y / norm, z / norm, w / norm
    result = np.eye(4, dtype=np.float64)
    result[:3, :3] = [
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ]
    result[:3, 3] = np.asarray(list(translation_m), dtype=np.float64)
    return matrix4(result)


def transform_record(matrix: np.ndarray) -> dict[str, object]:
    value = matrix4(matrix)
    return {
        "translation_m": value[:3, 3].tolist(),
        "rotation_xyzw": quaternion_xyzw(value[:3, :3]),
        "matrix": value.tolist(),
    }
