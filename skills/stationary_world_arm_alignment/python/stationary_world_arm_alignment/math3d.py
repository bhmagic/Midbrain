from __future__ import annotations

import math
from typing import Any, Iterable

import numpy as np


class YawUnobservableError(RuntimeError):
    """Report why a position-only base-yaw fit has insufficient leverage."""

    def __init__(self, diagnostics: dict[str, Any]):
        self.diagnostics = diagnostics
        predicted = float(diagnostics["predicted_horizontal_lever_arm_m"])
        observed = float(diagnostics["observed_horizontal_lever_arm_m"])
        minimum = float(diagnostics["minimum_horizontal_lever_arm_m"])
        super().__init__(
            "base yaw is unobservable from the current stationary pose: "
            f"controller TCP horizontal lever={predicted:.4f} m, "
            f"segmented gripper horizontal lever={observed:.4f} m, "
            f"required minimum={minimum:.4f} m. Move the end effector "
            "sideways away from the base Z axis while keeping the camera, "
            "base, and rig fixed, then retry calibration. No calibration "
            "candidate was created."
        )


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


def inspect_base_up_alignment(
    camera_from_base: np.ndarray,
    *,
    camera_system_up: np.ndarray | None,
    warning_tilt_deg: float = 10.0,
) -> dict[str, Any]:
    """Report base-up alignment without changing or rejecting the pose."""
    transform = np.asarray(camera_from_base, dtype=np.float64)
    if transform.shape != (4, 4) or not np.all(np.isfinite(transform)):
        raise ValueError("camera_from_base must be a finite 4x4 transform")
    threshold = float(warning_tilt_deg)
    if not 0.0 < threshold <= 90.0:
        raise ValueError("warning_tilt_deg must be in (0, 90]")
    if camera_system_up is None:
        return {
            "status": "WORLD_UP_UNAVAILABLE",
            "world_up_available": False,
            "within_warning_tolerance": None,
            "warning_tilt_deg": threshold,
            "warning": (
                "World/gravity up is unavailable; the FoundationPose base "
                "orientation was retained without modification."
            ),
            "transform_modified": False,
        }
    up = np.asarray(camera_system_up, dtype=np.float64)
    norm = float(np.linalg.norm(up))
    if up.shape != (3,) or not np.all(np.isfinite(up)) or norm <= 1e-12:
        return {
            "status": "WORLD_UP_UNAVAILABLE",
            "world_up_available": False,
            "within_warning_tolerance": None,
            "warning_tilt_deg": threshold,
            "warning": (
                "World/gravity up is invalid; the FoundationPose base "
                "orientation was retained without modification."
            ),
            "transform_modified": False,
        }
    up = up / norm
    dot = float(np.clip(np.dot(transform[:3, 2], up), -1.0, 1.0))
    tilt_deg = math.degrees(math.acos(dot))
    within = tilt_deg <= threshold
    return {
        "status": "ALIGNED" if within else "TILT_WARNING",
        "world_up_available": True,
        "world_up_axis_camera_system": up.tolist(),
        "base_z_dot_world_up": dot,
        "base_z_tilt_from_world_up_deg": tilt_deg,
        "within_warning_tolerance": within,
        "warning_tilt_deg": threshold,
        "warning": (
            None
            if within
            else (
                f"Base +Z is {tilt_deg:.2f} degrees from world/gravity up; "
                "the FoundationPose orientation was retained without "
                "modification."
            )
        ),
        "transform_modified": False,
    }


def select_base_orientation_correction(
    camera_from_base: Any,
    camera_system_up: Any | None,
    base_x_relation_to_gripper: str,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Select one principal-axis 180-degree hypothesis, applied at most once."""
    transform = np.asarray(camera_from_base, dtype=np.float64)
    if transform.shape != (4, 4) or not np.all(np.isfinite(transform)):
        raise ValueError("camera_from_base must be a finite 4x4 transform")
    relation = str(base_x_relation_to_gripper or "").strip().upper()
    if relation not in {
        "TOWARD_GRIPPER",
        "AWAY_FROM_GRIPPER",
        "UNCLEAR",
    }:
        raise ValueError("base_x_relation_to_gripper is invalid")

    up: np.ndarray | None = None
    if camera_system_up is not None:
        candidate_up = np.asarray(camera_system_up, dtype=np.float64)
        if candidate_up.shape == (3,) and np.all(np.isfinite(candidate_up)):
            up_norm = float(np.linalg.norm(candidate_up))
            if up_norm > 1e-12:
                up = candidate_up / up_norm
    raw_z_dot_up = (
        float(np.clip(np.dot(transform[:3, 2], up), -1.0, 1.0))
        if up is not None
        else None
    )
    needs_upright_flip = bool(
        raw_z_dot_up is not None and raw_z_dot_up < 0.0
    )
    needs_x_flip = relation == "AWAY_FROM_GRIPPER"
    selected_axis = {
        (False, False): "NONE",
        (False, True): "Z",
        (True, False): "X",
        (True, True): "Y",
    }[(needs_upright_flip, needs_x_flip)]
    rotations = {
        "NONE": np.eye(3, dtype=np.float64),
        "X": np.diag([1.0, -1.0, -1.0]),
        "Y": np.diag([-1.0, 1.0, -1.0]),
        "Z": np.diag([-1.0, -1.0, 1.0]),
    }
    correction = np.eye(4, dtype=np.float64)
    correction[:3, :3] = rotations[selected_axis]
    corrected = transform @ correction
    corrected_z_dot_up = (
        float(np.clip(np.dot(corrected[:3, 2], up), -1.0, 1.0))
        if up is not None
        else None
    )
    warnings: list[str] = []
    if up is None:
        warnings.append(
            "World/gravity up was unavailable, so the base +Z hemisphere "
            "could not be canonicalized."
        )
    if relation == "UNCLEAR":
        warnings.append(
            "The gripper relation was unclear, so raw base +X was retained."
        )
    if corrected_z_dot_up is not None and corrected_z_dot_up < -1e-9:
        raise AssertionError("base orientation selection left +Z below gravity-up")
    yaw_flip_deg = 180 if needs_x_flip else 0
    yaw_component = np.eye(4, dtype=np.float64)
    if needs_x_flip:
        yaw_component[:3, :3] = rotations["Z"]
    return correction, {
        "method": "SINGLE_DISCRETE_BASE_ORIENTATION_SELECTION",
        "selection_policy": (
            "UP_OK+X_TOWARD=NONE; UP_OK+X_AWAY=Z_180; "
            "UP_DOWN+X_TOWARD=X_180; UP_DOWN+X_AWAY=Y_180"
        ),
        "base_x_relation_to_gripper": relation,
        "selected_flip_deg": yaw_flip_deg,
        "fitted_yaw_rad": math.radians(yaw_flip_deg),
        "fitted_yaw_deg": float(yaw_flip_deg),
        "semantic_yaw_correction": yaw_component.tolist(),
        "yaw_correction_translation_norm_m": float(
            np.linalg.norm(yaw_component[:3, 3])
        ),
        "world_up_available": up is not None,
        "world_up_axis_camera_system": up.tolist() if up is not None else None,
        "raw_base_z_dot_world_up": raw_z_dot_up,
        "corrected_base_z_dot_world_up": corrected_z_dot_up,
        "upright_hemisphere_flip_required": needs_upright_flip,
        "selected_orientation_correction_axis": selected_axis,
        "selected_orientation_correction_deg": 0 if selected_axis == "NONE" else 180,
        "orientation_correction_count": 0 if selected_axis == "NONE" else 1,
        "semantic_orientation_correction": correction.tolist(),
        "orientation_correction_translation_norm_m": float(
            np.linalg.norm(correction[:3, 3])
        ),
        "warning": " ".join(warnings) if warnings else None,
        "consistency_passed": True,
    }


def select_base_orientation_from_gripper_point(
    camera_from_base: Any,
    camera_system_up: Any | None,
    camera_system_gripper_point_m: Any,
    *,
    weak_planar_baseline_m: float = 0.015,
    weak_forward_fraction: float = 0.2,
) -> tuple[np.ndarray, dict[str, Any], dict[str, Any]]:
    """Resolve the single discrete base orientation from up and gripper depth."""
    transform = np.asarray(camera_from_base, dtype=np.float64)
    point = np.asarray(camera_system_gripper_point_m, dtype=np.float64)
    if transform.shape != (4, 4) or not np.all(np.isfinite(transform)):
        raise ValueError("camera_from_base must be a finite 4x4 transform")
    if point.shape != (3,) or not np.all(np.isfinite(point)):
        raise ValueError(
            "camera_system_gripper_point_m must contain three finite values"
        )
    if weak_planar_baseline_m < 0.0:
        raise ValueError("weak_planar_baseline_m must be non-negative")
    if not 0.0 <= weak_forward_fraction <= 1.0:
        raise ValueError("weak_forward_fraction must be in [0, 1]")

    base_from_camera = np.linalg.inv(transform)
    base_point = (
        base_from_camera
        @ np.asarray([point[0], point[1], point[2], 1.0], dtype=np.float64)
    )[:3]
    planar_norm_m = float(np.linalg.norm(base_point[:2]))
    forward_component_m = float(base_point[0])
    forward_fraction = (
        abs(forward_component_m) / planar_norm_m
        if planar_norm_m > 1e-12
        else 0.0
    )
    relation = (
        "TOWARD_GRIPPER"
        if forward_component_m >= 0.0
        else "AWAY_FROM_GRIPPER"
    )
    correction, resolution = select_base_orientation_correction(
        transform,
        camera_system_up,
        relation,
    )
    weak_geometry = (
        planar_norm_m < weak_planar_baseline_m
        or forward_fraction < weak_forward_fraction
    )
    warnings = [str(resolution["warning"])] if resolution.get("warning") else []
    if weak_geometry:
        warnings.append(
            "The RGB-D gripper reference has weak horizontal yaw leverage; "
            "the exact 0/180-degree sign was retained as a warning."
        )
    resolution.update(
        {
            "method": "VLM_GRIPPER_RGBD_SINGLE_BASE_ORIENTATION_SELECTION",
            "selection_policy": (
                "RAW_BASE_X_DOT_GRIPPER_POSITIVE=0_DEG; "
                "NEGATIVE=180_DEG"
            ),
            "reference_source": (
                "VLM_GRIPPER_SEGMENTATION_WITH_ALIGNED_DEPTH"
            ),
            "gripper_point_in_raw_base_m": base_point.tolist(),
            "planar_baseline_m": planar_norm_m,
            "raw_base_x_component_m": forward_component_m,
            "raw_base_x_fraction_of_planar_baseline": forward_fraction,
            "weak_geometry": weak_geometry,
            "warning": " ".join(warnings) if warnings else None,
        }
    )
    axis_review = {
        "base_x_relation_to_gripper": relation,
        "notes": (
            "Aligned RGB-D places the gripper in the raw base +X half-space."
            if relation == "TOWARD_GRIPPER"
            else "Aligned RGB-D places the gripper in the raw base -X half-space."
        ),
    }
    return correction, axis_review, resolution


def apply_base_mesh_hypothesis_correction(
    parent_from_semantic: Any,
    mesh_from_semantic: Any,
    semantic_axis_correction: Any,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Apply one semantic-axis hypothesis at the centered CAD mesh origin.

    FoundationPose observes the centered mesh, while the published arm-base
    origin is related by ``mesh_from_semantic``. A discrete pose-hypothesis
    choice therefore has to be applied before that transform. Applying the
    same rotation after it would keep a potentially incorrect semantic origin
    fixed on the wrong side of the centered CAD geometry.
    """
    parent_from_base = np.asarray(parent_from_semantic, dtype=np.float64)
    mesh_from_base = np.asarray(mesh_from_semantic, dtype=np.float64)
    semantic_correction = np.asarray(
        semantic_axis_correction,
        dtype=np.float64,
    )
    for value, name in (
        (parent_from_base, "parent_from_semantic"),
        (mesh_from_base, "mesh_from_semantic"),
        (semantic_correction, "semantic_axis_correction"),
    ):
        if value.shape != (4, 4) or not np.all(np.isfinite(value)):
            raise ValueError(f"{name} must be a finite 4x4 transform")
        rotation = value[:3, :3]
        if not np.allclose(
            rotation.T @ rotation,
            np.eye(3),
            rtol=0.0,
            atol=1e-9,
        ) or not np.isclose(
            np.linalg.det(rotation),
            1.0,
            rtol=0.0,
            atol=1e-9,
        ):
            raise ValueError(f"{name} must contain a proper rotation")
        if not np.allclose(
            value[3],
            [0.0, 0.0, 0.0, 1.0],
            rtol=0.0,
            atol=1e-12,
        ):
            raise ValueError(f"{name} must be homogeneous")
    if not np.allclose(
        semantic_correction[:3, 3],
        0.0,
        rtol=0.0,
        atol=1e-12,
    ):
        raise ValueError("semantic_axis_correction must be rotation-only")

    semantic_from_mesh = np.linalg.inv(mesh_from_base)
    parent_from_mesh = parent_from_base @ semantic_from_mesh
    mesh_rotation = mesh_from_base[:3, :3]
    mesh_correction = np.eye(4, dtype=np.float64)
    mesh_correction[:3, :3] = (
        mesh_rotation
        @ semantic_correction[:3, :3]
        @ mesh_rotation.T
    )
    corrected = parent_from_mesh @ mesh_correction @ mesh_from_base
    corrected_parent_from_mesh = corrected @ semantic_from_mesh
    if not np.allclose(
        corrected_parent_from_mesh[:3, 3],
        parent_from_mesh[:3, 3],
        rtol=0.0,
        atol=1e-12,
    ):
        raise AssertionError(
            "base hypothesis selection moved the observed CAD mesh center"
        )
    expected_rotation = (
        parent_from_base[:3, :3]
        @ semantic_correction[:3, :3]
    )
    if not np.allclose(
        corrected[:3, :3],
        expected_rotation,
        rtol=0.0,
        atol=1e-9,
    ):
        raise AssertionError(
            "mesh-frame hypothesis selection produced incorrect semantic axes"
        )
    root_adjustment = corrected[:3, 3] - parent_from_base[:3, 3]
    semantic_application = (
        semantic_from_mesh @ mesh_correction @ mesh_from_base
    )
    return corrected, {
        "application_order": (
            "parent_from_mesh @ mesh_hypothesis_correction @ "
            "mesh_from_semantic"
        ),
        "application_origin": "FOUNDATIONPOSE_CENTERED_CAD_MESH_ORIGIN",
        "mesh_hypothesis_correction": mesh_correction.tolist(),
        "mesh_hypothesis_correction_translation_norm_m": float(
            np.linalg.norm(mesh_correction[:3, 3])
        ),
        "semantic_application_transform": semantic_application.tolist(),
        "semantic_root_translation_adjustment_m": root_adjustment.tolist(),
        "semantic_root_translation_adjustment_norm_m": float(
            np.linalg.norm(root_adjustment)
        ),
        "mesh_center_translation_preserved": True,
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
        [[cosine, -sine, 0.0], [sine, cosine, 0.0], [0.0, 0.0, 1.0]],
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
