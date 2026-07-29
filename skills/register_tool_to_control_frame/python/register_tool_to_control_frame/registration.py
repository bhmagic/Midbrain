from __future__ import annotations

from typing import Any

import numpy as np

from spatial_registration_rgbd import register_rgbd_point, transform_point


def _finite_transform(value: np.ndarray, name: str) -> np.ndarray:
    matrix = np.asarray(value, dtype=np.float64)
    if matrix.shape != (4, 4) or not np.all(np.isfinite(matrix)):
        raise ValueError(f"{name} must be a finite 4x4 transform")
    return matrix


def build_control_frame_candidate(
    *,
    landmarks_target_m: dict[str, list[float] | np.ndarray],
    target_from_tool: np.ndarray,
    geometry: dict[str, Any],
    landmark_confidences: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Construct a review-only control frame from three registered landmarks."""

    axis_start_role = str(geometry["axis_start_role"])
    axis_end_role = str(geometry["axis_end_role"])
    plane_role = str(geometry["plane_role"])
    required_roles = (axis_start_role, axis_end_role, plane_role)
    missing = [role for role in required_roles if role not in landmarks_target_m]
    if missing:
        raise ValueError(f"missing control-frame landmarks: {', '.join(missing)}")

    points = {
        role: np.asarray(landmarks_target_m[role], dtype=np.float64)
        for role in required_roles
    }
    if not all(point.shape == (3,) and np.all(np.isfinite(point)) for point in points.values()):
        raise ValueError("control-frame landmarks must be finite 3-vectors")

    target_from_tool_matrix = _finite_transform(
        target_from_tool,
        "target_from_tool",
    )
    tool_from_target = np.linalg.inv(target_from_tool_matrix)
    axis_vector = points[axis_end_role] - points[axis_start_role]
    axis_length_m = float(np.linalg.norm(axis_vector))
    if axis_length_m <= 1e-12:
        raise ValueError("axis landmarks are degenerate")
    x_axis_target = axis_vector / axis_length_m

    plane_vector = points[plane_role] - points[axis_start_role]
    plane_perpendicular = (
        plane_vector - float(plane_vector @ x_axis_target) * x_axis_target
    )
    plane_offset_m = float(np.linalg.norm(plane_perpendicular))
    if plane_offset_m <= 1e-12:
        raise ValueError("the plane landmark is collinear with the axis")
    plane_sign = float(geometry.get("plane_axis_sign", 1.0))
    if plane_sign == 0.0:
        raise ValueError("plane_axis_sign must not be zero")
    z_axis_target = np.sign(plane_sign) * plane_perpendicular / plane_offset_m
    y_axis_target = np.cross(z_axis_target, x_axis_target)
    y_axis_target /= np.linalg.norm(y_axis_target)
    z_axis_target = np.cross(x_axis_target, y_axis_target)
    z_axis_target /= np.linalg.norm(z_axis_target)
    target_from_control_rotation = np.column_stack(
        [x_axis_target, y_axis_target, z_axis_target]
    )

    origin_distance_m = float(geometry.get("origin_from_axis_start_m", 0.0))
    origin_target = points[axis_start_role] + origin_distance_m * x_axis_target
    origin_tool = transform_point(tool_from_target, origin_target)
    tool_from_control_rotation = (
        tool_from_target[:3, :3] @ target_from_control_rotation
    )
    tool_from_control = np.eye(4, dtype=np.float64)
    tool_from_control[:3, :3] = tool_from_control_rotation
    tool_from_control[:3, 3] = origin_tool

    confidences = landmark_confidences or {}
    minimum_confidence = float(geometry.get("minimum_landmark_confidence", 0.0))
    minimum_axis_length_m = float(geometry.get("minimum_axis_length_m", 0.0))
    minimum_plane_offset_m = float(geometry.get("minimum_plane_offset_m", 0.0))
    maximum_tool_to_origin_m = float(
        geometry.get("maximum_tool_to_origin_m", np.inf)
    )
    tool_to_origin_m = float(np.linalg.norm(origin_tool))
    reasons: list[str] = []
    low_confidence_roles = [
        role
        for role in required_roles
        if float(confidences.get(role, 0.0)) < minimum_confidence
    ]
    if low_confidence_roles:
        reasons.append(
            "VLM landmark confidence is too low for: "
            + ", ".join(low_confidence_roles)
        )
    if axis_length_m < minimum_axis_length_m:
        reasons.append("registered tool axis is shorter than its physical limit")
    if plane_offset_m < minimum_plane_offset_m:
        reasons.append("registered plane landmark is too close to the tool axis")
    if tool_to_origin_m > maximum_tool_to_origin_m:
        reasons.append("tool-to-control-frame distance exceeds its physical limit")

    eligible = not reasons
    return {
        "schema": "physical_agent.tool_control_frame_candidate",
        "schema_version": 1,
        "status": (
            "CANDIDATE_AUTHORIZATION_REQUIRED"
            if eligible
            else "REJECTED_OBSERVATION"
        ),
        "motion_usable": False,
        "operator_review_required": True,
        "eligible_for_authorization": eligible,
        "publishes_control_frame": False,
        "activation_requires_separate_authorization": True,
        "tool_from_control_frame": tool_from_control.tolist(),
        "control_origin_from_tool_m": origin_tool.tolist(),
        "control_rotation_matrix_from_tool": tool_from_control_rotation.tolist(),
        "geometry": {
            "axis_start_role": axis_start_role,
            "axis_end_role": axis_end_role,
            "plane_role": plane_role,
            "origin_from_axis_start_m": origin_distance_m,
            "plane_axis_sign": float(np.sign(plane_sign)),
        },
        "quality_metrics": {
            "axis_length_m": axis_length_m,
            "plane_perpendicular_offset_m": plane_offset_m,
            "tool_to_control_origin_m": tool_to_origin_m,
            "landmark_confidences": {
                role: float(confidences.get(role, 0.0))
                for role in required_roles
            },
        },
        "quality_reasons": reasons,
    }


def register_tool_to_control_frame_candidate(
    *,
    vlm_result: dict[str, Any],
    rgb_grid: tuple[int, int],
    registered_depth_m: np.ndarray,
    registered_depth_grid: tuple[int, int],
    intrinsics: dict[str, Any],
    target_from_camera: np.ndarray,
    target_from_tool: np.ndarray,
    observed_at_us: int,
    source_frame: str,
    target_frame: str,
    calibration_revision: str | None,
    route_provenance: dict[str, Any],
    geometry: dict[str, Any],
    search_radius_px: int = 3,
    valid_region: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Register VLM landmarks and build a non-published tool-frame candidate."""

    landmarks = vlm_result.get("landmarks")
    if not isinstance(landmarks, list) or not landmarks:
        raise ValueError("vlm_result.landmarks must be a non-empty array")
    registrations: dict[str, dict[str, Any]] = {}
    confidences: dict[str, float] = {}
    for landmark in landmarks:
        if not isinstance(landmark, dict):
            raise ValueError("each VLM landmark must be an object")
        role = str(landmark.get("role") or "")
        if not role or role in registrations:
            raise ValueError("VLM landmark roles must be non-empty and unique")
        pixel = landmark.get("pixel_yx")
        if not isinstance(pixel, (list, tuple)) or len(pixel) != 2:
            raise ValueError(f"VLM landmark {role} must contain pixel_yx")
        registration = register_rgbd_point(
            rgb_pixel_yx=(float(pixel[0]), float(pixel[1])),
            rgb_grid=rgb_grid,
            registered_depth_m=registered_depth_m,
            registered_depth_grid=registered_depth_grid,
            intrinsics=intrinsics,
            target_from_camera=target_from_camera,
            observed_at_us=observed_at_us,
            source_frame=source_frame,
            target_frame=target_frame,
            calibration_revision=calibration_revision,
            route_provenance=route_provenance,
            depth_policy=str(
                landmark.get("depth_policy") or "ROBUST_MEDIAN"
            ),
            search_radius_px=search_radius_px,
            valid_region=valid_region,
        )
        registrations[role] = registration
        confidences[role] = float(landmark.get("confidence") or 0.0)

    candidate = build_control_frame_candidate(
        landmarks_target_m={
            role: registration["target_point_m"]
            for role, registration in registrations.items()
        },
        target_from_tool=target_from_tool,
        geometry=geometry,
        landmark_confidences=confidences,
    )
    candidate["observed_at_us"] = int(observed_at_us)
    candidate["target_frame"] = str(target_frame)
    candidate["calibration_revision"] = calibration_revision
    candidate["registered_landmarks"] = registrations
    candidate["vlm_provenance"] = {
        key: vlm_result.get(key)
        for key in ("backend_id", "model", "request_id")
        if vlm_result.get(key) is not None
    }
    candidate["data_route"] = dict(route_provenance)
    return candidate
