from __future__ import annotations

import math
from typing import Any, Iterable

import numpy as np


def normalize_quaternion_xyzw(value: Any) -> np.ndarray:
    quaternion = np.asarray(value, dtype=np.float64)
    if quaternion.shape != (4,) or not np.all(np.isfinite(quaternion)):
        raise ValueError("quaternion must contain four finite XYZW values")
    norm = float(np.linalg.norm(quaternion))
    if norm <= 1e-12:
        raise ValueError("quaternion norm is zero")
    return quaternion / norm


def quaternion_xyzw_to_matrix(value: Any) -> np.ndarray:
    x, y, z, w = normalize_quaternion_xyzw(value)
    return np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ],
        dtype=np.float64,
    )


def matrix_to_quaternion_xyzw(rotation: Any) -> np.ndarray:
    matrix = np.asarray(rotation, dtype=np.float64)
    if matrix.shape != (3, 3) or not np.all(np.isfinite(matrix)):
        raise ValueError("rotation must be a finite 3x3 matrix")
    trace = float(np.trace(matrix))
    if trace > 0:
        scale = math.sqrt(trace + 1.0) * 2
        w = 0.25 * scale
        x = (matrix[2, 1] - matrix[1, 2]) / scale
        y = (matrix[0, 2] - matrix[2, 0]) / scale
        z = (matrix[1, 0] - matrix[0, 1]) / scale
    elif matrix[0, 0] > matrix[1, 1] and matrix[0, 0] > matrix[2, 2]:
        scale = math.sqrt(1 + matrix[0, 0] - matrix[1, 1] - matrix[2, 2]) * 2
        w = (matrix[2, 1] - matrix[1, 2]) / scale
        x = 0.25 * scale
        y = (matrix[0, 1] + matrix[1, 0]) / scale
        z = (matrix[0, 2] + matrix[2, 0]) / scale
    elif matrix[1, 1] > matrix[2, 2]:
        scale = math.sqrt(1 + matrix[1, 1] - matrix[0, 0] - matrix[2, 2]) * 2
        w = (matrix[0, 2] - matrix[2, 0]) / scale
        x = (matrix[0, 1] + matrix[1, 0]) / scale
        y = 0.25 * scale
        z = (matrix[1, 2] + matrix[2, 1]) / scale
    else:
        scale = math.sqrt(1 + matrix[2, 2] - matrix[0, 0] - matrix[1, 1]) * 2
        w = (matrix[1, 0] - matrix[0, 1]) / scale
        x = (matrix[0, 2] + matrix[2, 0]) / scale
        y = (matrix[1, 2] + matrix[2, 1]) / scale
        z = 0.25 * scale
    return normalize_quaternion_xyzw([x, y, z, w])


def transform_matrix(translation_m: Any, rotation_xyzw: Any) -> np.ndarray:
    translation = np.asarray(translation_m, dtype=np.float64)
    if translation.shape != (3,) or not np.all(np.isfinite(translation)):
        raise ValueError("translation must contain three finite metres")
    result = np.eye(4, dtype=np.float64)
    result[:3, :3] = quaternion_xyzw_to_matrix(rotation_xyzw)
    result[:3, 3] = translation
    return result


def transform_payload(matrix: Any) -> dict[str, list[float]]:
    value = np.asarray(matrix, dtype=np.float64)
    if value.shape != (4, 4) or not np.all(np.isfinite(value)):
        raise ValueError("transform must be a finite 4x4 matrix")
    return {
        "translation_m": value[:3, 3].tolist(),
        "rotation_xyzw": matrix_to_quaternion_xyzw(value[:3, :3]).tolist(),
    }


def transform_from_payload(payload: dict[str, Any]) -> np.ndarray:
    rotation = payload.get("rotation_xyzw") or payload.get("quaternion_xyzw")
    if rotation is None:
        raise ValueError("transform payload has no XYZW quaternion")
    return transform_matrix(payload["translation_m"], rotation)


def apply_transform(matrix: Any, point_xyz: Any) -> np.ndarray:
    transform = np.asarray(matrix, dtype=np.float64)
    point = np.asarray(point_xyz, dtype=np.float64)
    if transform.shape != (4, 4) or point.shape != (3,):
        raise ValueError("expected a 4x4 transform and one 3D point")
    return transform[:3, :3] @ point + transform[:3, 3]


def base_yaw_flip() -> np.ndarray:
    result = np.eye(4, dtype=np.float64)
    result[:3, :3] = np.diag([-1.0, -1.0, 1.0])
    return result


def base_upright_correction(
    world_from_base: np.ndarray,
    *,
    world_up: np.ndarray | None = None,
) -> tuple[np.ndarray, dict[str, Any]]:
    transform = np.asarray(world_from_base, dtype=np.float64)
    if transform.shape != (4, 4) or not np.all(np.isfinite(transform)):
        raise ValueError("world_from_base must be a finite 4x4 transform")
    up = np.asarray(
        [0.0, 1.0, 0.0] if world_up is None else world_up,
        dtype=np.float64,
    )
    up /= np.linalg.norm(up)
    raw_alignment = float(np.dot(transform[:3, 2], up))
    correction = np.eye(4, dtype=np.float64)
    correction_name = "NONE"
    if raw_alignment < 0:
        # A 180-degree semantic X rotation makes base +Z upright while leaving
        # the later 0/180-degree yaw decision free to choose positive base X.
        correction[:3, :3] = np.diag([1.0, -1.0, -1.0])
        correction_name = "SEMANTIC_X_180"
    corrected = transform @ correction
    corrected_alignment = float(np.dot(corrected[:3, 2], up))
    return correction, {
        "world_up_axis": up.tolist(),
        "raw_base_z_dot_world_up": raw_alignment,
        "corrected_base_z_dot_world_up": corrected_alignment,
        "correction": correction_name,
        "correction_applied": correction_name != "NONE",
    }


def closest_pair_consensus(points: Iterable[Any]) -> tuple[np.ndarray, dict[str, Any]]:
    vectors = [np.asarray(point, dtype=np.float64) for point in points]
    if len(vectors) != 3 or any(
        point.shape != (3,) or not np.all(np.isfinite(point)) for point in vectors
    ):
        raise ValueError("closest-pair consensus requires exactly three finite 3D points")
    pairs = ((0, 1), (0, 2), (1, 2))
    distances = [
        float(np.linalg.norm(vectors[first] - vectors[second]))
        for first, second in pairs
    ]
    selected_index = int(np.argmin(distances))
    first, second = pairs[selected_index]
    return (vectors[first] + vectors[second]) / 2.0, {
        "selected_indices": [first, second],
        "selected_pair_distance_m": distances[selected_index],
        "pair_distances_m": {
            f"{left}-{right}": distance
            for (left, right), distance in zip(pairs, distances, strict=True)
        },
    }


def world_up_yaw(delta_rad: float) -> np.ndarray:
    cosine, sine = math.cos(delta_rad), math.sin(delta_rad)
    result = np.eye(4, dtype=np.float64)
    result[:3, :3] = np.array(
        [[cosine, 0.0, sine], [0.0, 1.0, 0.0], [-sine, 0.0, cosine]],
        dtype=np.float64,
    )
    return result


def quaternion_angular_distance(left: Any, right: Any) -> float:
    q_left = normalize_quaternion_xyzw(left)
    q_right = normalize_quaternion_xyzw(right)
    dot = min(1.0, abs(float(np.dot(q_left, q_right))))
    return 2.0 * math.acos(dot)


def average_quaternions_xyzw(values: Iterable[Any]) -> np.ndarray:
    quaternions = [normalize_quaternion_xyzw(value) for value in values]
    if not quaternions:
        raise ValueError("at least one quaternion is required")
    reference = quaternions[0]
    aligned = [value if float(np.dot(value, reference)) >= 0 else -value for value in quaternions]
    accumulator = sum(np.outer(value, value) for value in aligned)
    eigenvalues, eigenvectors = np.linalg.eigh(accumulator)
    result = eigenvectors[:, int(np.argmax(eigenvalues))]
    if float(np.dot(result, reference)) < 0:
        result = -result
    return normalize_quaternion_xyzw(result)


def robust_average_transforms(values: Iterable[Any]) -> tuple[np.ndarray, dict[str, Any]]:
    transforms = [np.asarray(value, dtype=np.float64) for value in values]
    if not transforms:
        raise ValueError("at least one transform is required")
    if any(value.shape != (4, 4) or not np.all(np.isfinite(value)) for value in transforms):
        raise ValueError("all transforms must be finite 4x4 matrices")

    translations = np.stack([value[:3, 3] for value in transforms])
    median = np.median(translations, axis=0)
    distances = np.linalg.norm(translations - median, axis=1)
    distance_median = float(np.median(distances))
    mad = float(np.median(np.abs(distances - distance_median)))
    threshold = max(0.01, distance_median + 3.5 * max(mad, 1e-6))
    keep = distances <= threshold
    if not np.any(keep):
        keep[:] = True
    retained = [value for value, accepted in zip(transforms, keep, strict=True) if accepted]
    translation = np.median(np.stack([value[:3, 3] for value in retained]), axis=0)
    quaternions = [matrix_to_quaternion_xyzw(value[:3, :3]) for value in retained]
    quaternion = average_quaternions_xyzw(quaternions)

    output = transform_matrix(translation, quaternion)
    rotation_errors = [
        quaternion_angular_distance(quaternion, matrix_to_quaternion_xyzw(value[:3, :3]))
        for value in retained
    ]
    diagnostics = {
        "input_count": len(transforms),
        "retained_count": len(retained),
        "translation_mad_m": mad,
        "translation_max_residual_m": float(
            max(np.linalg.norm(value[:3, 3] - translation) for value in retained)
        ),
        "rotation_median_residual_rad": float(np.median(rotation_errors)),
        "rotation_max_residual_rad": float(max(rotation_errors)),
    }
    return output, diagnostics


def choose_base_symmetry(
    world_from_base: np.ndarray,
    world_beak: np.ndarray,
    *,
    base_tool_point: np.ndarray | None,
) -> tuple[np.ndarray, dict[str, Any]]:
    candidates = [world_from_base, world_from_base @ base_yaw_flip()]
    if base_tool_point is not None:
        predicted = [apply_transform(candidate, base_tool_point) for candidate in candidates]
        scores = [float(np.linalg.norm(point - world_beak)) for point in predicted]
        selected = int(np.argmin(scores))
        margin = abs(scores[1] - scores[0])
        method = "KINEMATIC_TOOL_TO_VLM_BEAK"
    else:
        vector = world_beak - world_from_base[:3, 3]
        scores = [
            -float(np.dot(candidate[:3, 0], vector))
            for candidate in candidates
        ]
        selected = int(np.argmin(scores))
        margin = abs(scores[1] - scores[0])
        predicted = [None, None]
        method = "POSITIVE_BASE_X_TO_VLM_BEAK"
    return candidates[selected], {
        "selected_flip_deg": 180 if selected else 0,
        "candidate_scores": scores,
        "score_margin": margin,
        "method": method,
        "predicted_tool_points_world": [
            None if point is None else point.tolist() for point in predicted
        ],
    }


def deproject_pixel(pixel_yx: Any, depth_m: float, intrinsics: dict[str, Any]) -> np.ndarray:
    y, x = (float(value) for value in pixel_yx)
    fx = float(intrinsics.get("fx", 0.0))
    fy = float(intrinsics.get("fy", 0.0))
    cx = float(intrinsics.get("cx", 0.0))
    cy = float(intrinsics.get("cy", 0.0))
    if fx <= 0 or fy <= 0 or not math.isfinite(depth_m) or depth_m <= 0:
        raise ValueError("valid intrinsics and positive finite depth are required")
    return np.array(
        [(x - cx) * depth_m / fx, (y - cy) * depth_m / fy, depth_m],
        dtype=np.float64,
    )
