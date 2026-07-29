from __future__ import annotations

from typing import Any

import numpy as np

from .math3d import matrix_rpy, rotation_angle_rad, transform_points


def evaluate_blade_registration_consistency(
    candidates: list[dict[str, Any]],
    config: dict[str, Any],
) -> dict[str, Any]:
    required = int(config["required_observations"])
    if required < 1:
        raise ValueError("blade registration requires at least one observation")
    window = candidates[-required:]
    valid = [
        candidate
        for candidate in window
        if (
            candidate.get("status") == "CANDIDATE_REVIEW_REQUIRED"
            and candidate.get("acting_point_from_tool_m") is not None
        )
    ]
    reasons: list[str] = []
    metrics: dict[str, Any] = {}
    representative: list[float] | None = None
    representative_rotation: list[list[float]] | None = None
    representative_rpy: list[float] | None = None

    if len(candidates) < required:
        status = "MORE_OBSERVATIONS_REQUIRED"
        reasons.append(
            f"{required - len(candidates)} additional blade observation(s) are required"
        )
    elif len(valid) != required:
        status = "OBSERVATION_WINDOW_REJECTED"
        reasons.append("every observation in the current window must pass quality gates")
    else:
        acting_points = np.asarray(
            [candidate["acting_point_from_tool_m"] for candidate in valid],
            dtype=np.float64,
        )
        representative_array = np.median(acting_points, axis=0)
        deviations_mm = (
            np.linalg.norm(acting_points - representative_array, axis=1) * 1000.0
        )
        edge_lengths_mm = np.asarray(
            [candidate["quality_metrics"]["edge_length_mm"] for candidate in valid],
            dtype=np.float64,
        )
        blade_widths_mm = np.asarray(
            [
                candidate["quality_metrics"]["spine_perpendicular_mm"]
                for candidate in valid
            ],
            dtype=np.float64,
        )
        rotations = [
            np.asarray(
                candidate["controlled_frame_rotation_matrix_from_tool"],
                dtype=np.float64,
            )
            for candidate in valid
        ]
        pairwise_angles = np.asarray(
            [
                [
                    rotation_angle_rad(first, second)
                    for second in rotations
                ]
                for first in rotations
            ],
            dtype=np.float64,
        )
        medoid_index = int(np.argmin(np.sum(pairwise_angles, axis=1)))
        representative_rotation_array = rotations[medoid_index]
        orientation_deviations_deg = np.degrees(
            pairwise_angles[medoid_index]
        )
        maximum_deviation_mm = float(np.max(deviations_mm))
        edge_length_range_mm = float(np.ptp(edge_lengths_mm))
        blade_width_range_mm = float(np.ptp(blade_widths_mm))
        maximum_orientation_deviation_deg = float(
            np.max(orientation_deviations_deg)
        )
        representative = representative_array.tolist()
        representative_rotation = representative_rotation_array.tolist()
        representative_rpy = matrix_rpy(
            representative_rotation_array
        ).tolist()
        metrics = {
            "maximum_acting_point_deviation_mm": maximum_deviation_mm,
            "edge_length_range_mm": edge_length_range_mm,
            "blade_width_range_mm": blade_width_range_mm,
            "acting_point_deviations_mm": deviations_mm.tolist(),
            "maximum_orientation_deviation_deg": (
                maximum_orientation_deviation_deg
            ),
            "orientation_deviations_deg": orientation_deviations_deg.tolist(),
        }
        if maximum_deviation_mm > float(
            config["maximum_acting_point_deviation_mm"]
        ):
            reasons.append("tool-frame acting-point observations are too far apart")
        if edge_length_range_mm > float(config["maximum_edge_length_range_mm"]):
            reasons.append("observed blade edge lengths are inconsistent")
        if blade_width_range_mm > float(config["maximum_blade_width_range_mm"]):
            reasons.append("observed blade widths are inconsistent")
        if maximum_orientation_deviation_deg > float(
            config["maximum_orientation_deviation_deg"]
        ):
            reasons.append("observed blade-frame orientations are inconsistent")
        status = (
            (
                "SINGLE_OBSERVATION_REVIEW_REQUIRED"
                if required == 1
                else "CONSISTENT_REVIEW_REQUIRED"
            )
            if not reasons
            else "INCONSISTENT_OBSERVATIONS"
        )

    reviewable_statuses = {
        "SINGLE_OBSERVATION_REVIEW_REQUIRED",
        "CONSISTENT_REVIEW_REQUIRED",
    }
    return {
        "status": status,
        "motion_usable": False,
        "operator_review_required": True,
        "eligible_for_operator_review": status in reviewable_statuses,
        "required_observations": required,
        "total_observations": len(candidates),
        "window_observations": len(window),
        "valid_window_observations": len(valid),
        "representative_acting_point_from_tool_m": representative,
        "representative_controlled_frame_rotation_matrix_from_tool": (
            representative_rotation
        ),
        "representative_controlled_frame_rpy_from_tool": representative_rpy,
        "quality_metrics": metrics,
        "quality_reasons": reasons,
    }


def build_blade_registration_candidate(
    blade_observation: dict[str, Any],
    arm_from_tool: np.ndarray,
    blade_image_points_yx_1000: dict[str, list[int]],
    config: dict[str, Any],
) -> dict[str, Any]:
    """Build a review-only tool-frame acting-point candidate from one blade observation."""
    arm_points = {
        name: np.asarray(
            blade_observation["arm_base_points_m"][name],
            dtype=np.float64,
        )
        for name in ("tip", "heel", "spine")
    }
    camera_points = {
        name: np.asarray(
            blade_observation["camera_points_m"][name],
            dtype=np.float64,
        )
        for name in ("tip", "heel", "spine")
    }
    matrix = np.asarray(arm_from_tool, dtype=np.float64)
    if matrix.shape != (4, 4) or not np.all(np.isfinite(matrix)):
        raise ValueError("arm_from_tool must be a finite 4x4 transform")
    if not all(
        point.shape == (3,) and np.all(np.isfinite(point))
        for point in [*arm_points.values(), *camera_points.values()]
    ):
        raise ValueError("blade registration points must be finite 3-vectors")

    tip_to_heel = arm_points["heel"] - arm_points["tip"]
    edge_length_m = float(np.linalg.norm(tip_to_heel))
    if edge_length_m <= 1e-9:
        raise ValueError("blade tip and heel are degenerate")
    tip_to_heel_axis = tip_to_heel / edge_length_m
    heel_to_tip_axis = -tip_to_heel_axis
    spine_from_heel = arm_points["spine"] - arm_points["heel"]
    spine_along_edge_m = float(spine_from_heel @ heel_to_tip_axis)
    spine_perpendicular = (
        spine_from_heel - spine_along_edge_m * heel_to_tip_axis
    )
    spine_perpendicular_m = float(np.linalg.norm(spine_perpendicular))
    depth_values = np.asarray(
        [camera_points[name][2] for name in ("tip", "heel", "spine")],
        dtype=np.float64,
    )
    depth_spread_m = float(np.max(depth_values) - np.min(depth_values))

    acting_distance_m = float(config["acting_point_from_tip_mm"]) / 1000.0
    acting_point_arm = arm_points["tip"] + acting_distance_m * tip_to_heel_axis
    tool_from_arm = np.linalg.inv(matrix)
    tool_points = {
        name: transform_points(tool_from_arm, point).tolist()
        for name, point in arm_points.items()
    }
    acting_point_tool = transform_points(tool_from_arm, acting_point_arm)
    tool_to_acting_point_m = float(np.linalg.norm(acting_point_tool))
    tool_points_array = {
        name: np.asarray(point, dtype=np.float64)
        for name, point in tool_points.items()
    }
    edge_axis_tool = tool_points_array["heel"] - tool_points_array["tip"]
    edge_axis_tool /= np.linalg.norm(edge_axis_tool)
    spine_from_tip_tool = (
        tool_points_array["spine"] - tool_points_array["tip"]
    )
    spine_perpendicular_tool = (
        spine_from_tip_tool
        - float(spine_from_tip_tool @ edge_axis_tool) * edge_axis_tool
    )
    if float(np.linalg.norm(spine_perpendicular_tool)) <= 1e-9:
        raise ValueError("blade spine and cutting edge do not define an orientation")
    down_axis_tool = -spine_perpendicular_tool / np.linalg.norm(
        spine_perpendicular_tool
    )
    normal_axis_tool = np.cross(down_axis_tool, edge_axis_tool)
    normal_axis_tool /= np.linalg.norm(normal_axis_tool)
    down_axis_tool = np.cross(edge_axis_tool, normal_axis_tool)
    down_axis_tool /= np.linalg.norm(down_axis_tool)
    controlled_rotation_from_tool = np.column_stack(
        [edge_axis_tool, normal_axis_tool, down_axis_tool]
    )
    controlled_rpy_from_tool = matrix_rpy(
        controlled_rotation_from_tool
    ).tolist()

    maximum_local_depth_range_mm = float(
        config["maximum_local_depth_range_mm"]
    )
    maximum_tool_distance_m = float(config["maximum_tool_to_acting_point_m"])
    tool_forward_axis = np.asarray(
        config["tool_forward_axis_xyz"],
        dtype=np.float64,
    )
    tool_forward_axis /= np.linalg.norm(tool_forward_axis)
    tool_to_acting_direction = (
        acting_point_tool / tool_to_acting_point_m
        if tool_to_acting_point_m > 1e-9
        else np.zeros(3, dtype=np.float64)
    )
    tool_forward_axis_cosine = float(
        tool_forward_axis @ tool_to_acting_direction
    )
    minimum_remaining_edge_m = (
        float(config["minimum_remaining_edge_after_acting_point_mm"]) / 1000.0
    )

    reasons: list[str] = []
    if acting_distance_m + minimum_remaining_edge_m > edge_length_m:
        reasons.append("the requested acting point does not fit on the observed edge")
    if not -0.25 * edge_length_m <= spine_along_edge_m <= 1.25 * edge_length_m:
        reasons.append("spine point lies too far beyond the observed blade edge")
    depth_diagnostics = blade_observation.get("depth_diagnostics") or {}
    local_depth_ranges_mm = {
        name: (
            float(values.get("p90_m") or 0.0)
            - float(values.get("p10_m") or 0.0)
        )
        * 1000.0
        for name, values in depth_diagnostics.items()
        if isinstance(values, dict)
    }
    reflective_fallback_used = bool(
        (blade_observation.get("depth_geometry") or {}).get(
            "reflective_fallback_used"
        )
    )
    if any(
        value > maximum_local_depth_range_mm
        for value in local_depth_ranges_mm.values()
    ) and not reflective_fallback_used:
        reasons.append(
            "a blade landmark local depth patch exceeds the configured range"
        )
    if tool_to_acting_point_m > maximum_tool_distance_m:
        reasons.append("tool-to-acting-point distance exceeds the configured physical limit")
    ratio = acting_distance_m / edge_length_m
    tip_yx = np.asarray(blade_image_points_yx_1000["tip"], dtype=np.float64)
    heel_yx = np.asarray(blade_image_points_yx_1000["heel"], dtype=np.float64)
    acting_yx = np.clip(
        np.rint(tip_yx + ratio * (heel_yx - tip_yx)),
        0,
        1000,
    ).astype(int)
    eligible = not reasons
    return {
        "status": (
            "CANDIDATE_REVIEW_REQUIRED"
            if eligible
            else "REJECTED_OBSERVATION"
        ),
        "source": "VLM_RGBD_BLADE_POINTS_PLUS_TOOL_TRANSFORM",
        "eligible_for_operator_review": eligible,
        "motion_usable": False,
        "acting_point_from_tip_mm": acting_distance_m * 1000.0,
        "acting_point_image_yx_1000": acting_yx.tolist(),
        "acting_point_arm_base_m": acting_point_arm.tolist(),
        "acting_point_from_tool_m": acting_point_tool.tolist(),
        "tip_from_tool_m": tool_points["tip"],
        "heel_from_tool_m": tool_points["heel"],
        "spine_from_tool_m": tool_points["spine"],
        "controlled_frame_rotation_matrix_from_tool": (
            controlled_rotation_from_tool.tolist()
        ),
        "controlled_frame_rpy_from_tool": controlled_rpy_from_tool,
        "orientation_status": "THREE_POINT_BLADE_FRAME_CANDIDATE",
        "quality_metrics": {
            "edge_length_mm": edge_length_m * 1000.0,
            "spine_perpendicular_mm": spine_perpendicular_m * 1000.0,
            "spine_along_edge_mm": spine_along_edge_m * 1000.0,
            "depth_spread_mm": depth_spread_m * 1000.0,
            "depth_spread_semantics": (
                "INFORMATIONAL_PHYSICAL_PERSPECTIVE_NOT_A_REJECTION_GATE"
            ),
            "local_depth_ranges_mm": local_depth_ranges_mm,
            "raw_blade_depth_range_gate_enabled": not reflective_fallback_used,
            "spine_depth_interpolated": bool(
                (blade_observation.get("depth_geometry") or {}).get(
                    "spine_depth_interpolated"
                )
            ),
            "spine_raw_interpolation_residual_mm": (
                blade_observation.get("depth_geometry") or {}
            ).get("spine_raw_interpolation_residual_mm"),
            "spine_correction_exceeds_review_limit": bool(
                (blade_observation.get("depth_geometry") or {}).get(
                    "spine_correction_exceeds_review_limit"
                )
            ),
            "tool_to_acting_point_mm": tool_to_acting_point_m * 1000.0,
            "tool_forward_axis_xyz": tool_forward_axis.tolist(),
            "tool_forward_axis_cosine": tool_forward_axis_cosine,
            "tool_forward_axis_gate_enabled": False,
            "edge_length_gate_enabled": False,
            "blade_width_gate_enabled": False,
        },
        "quality_reasons": reasons,
        "review_requirements": [
            "compare the overlay acting point with the physical blade",
            "measure payload mass and tool-frame center of mass",
            "review the three-point blade-frame orientation before POSE_6DOF execution",
        ],
    }
