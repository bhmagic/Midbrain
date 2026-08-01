from __future__ import annotations

import numpy as np
import pytest

from vegetable_cutting.camera import RgbdFrame
from vegetable_cutting.config import load_skill_config
from vegetable_cutting.math3d import (
    normalized_yx_to_pixel,
    transform_matrix,
    transform_points,
)
from vegetable_cutting.skill import VegetableCuttingSkill
from vegetable_cutting.tool_registration import (
    build_blade_registration_candidate,
    evaluate_blade_registration_consistency,
)


def registration_config() -> dict[str, float]:
    return {
        "acting_point_from_tip_mm": 50.0,
        "minimum_edge_length_mm": 80.0,
        "maximum_edge_length_mm": 350.0,
        "minimum_remaining_edge_after_acting_point_mm": 10.0,
        "minimum_blade_width_mm": 3.0,
        "maximum_blade_width_mm": 90.0,
        "maximum_local_depth_range_mm": 80.0,
        "maximum_tool_to_acting_point_m": 0.8,
        "tool_forward_axis_xyz": [1.0, 0.0, 0.0],
        "minimum_tool_forward_axis_cosine": 0.25,
    }


def image_points() -> dict[str, list[int]]:
    return {
        "tip": [800, 500],
        "heel": [500, 500],
        "spine": [650, 560],
    }


def consistency_config() -> dict[str, float | int]:
    return {
        "required_observations": 3,
        "maximum_acting_point_deviation_mm": 8.0,
        "maximum_edge_length_range_mm": 15.0,
        "maximum_blade_width_range_mm": 12.0,
        "maximum_orientation_deviation_deg": 8.0,
    }


def consistency_candidate(
    acting_point_x_m: float,
    *,
    status: str = "CANDIDATE_REVIEW_REQUIRED",
) -> dict[str, object]:
    return {
        "status": status,
        "acting_point_from_tool_m": [acting_point_x_m, 0.1, 0.1],
        "controlled_frame_rotation_matrix_from_tool": np.eye(3).tolist(),
        "quality_metrics": {
            "edge_length_mm": 200.0 + acting_point_x_m * 10.0,
            "spine_perpendicular_mm": 40.0 + acting_point_x_m * 5.0,
        },
    }


def test_registration_candidate_places_acting_point_five_cm_from_tip() -> None:
    observation = {
        "arm_base_points_m": {
            "tip": [0.10, 0.20, 0.30],
            "heel": [0.30, 0.20, 0.30],
            "spine": [0.20, 0.24, 0.30],
        },
        "camera_points_m": {
            "tip": [0.0, 0.0, 1.00],
            "heel": [0.0, 0.0, 1.01],
            "spine": [0.0, 0.0, 1.02],
        },
        "depth_diagnostics": {
            name: {"p10_m": 1.0, "p90_m": 1.01}
            for name in ("tip", "heel", "spine")
        },
    }
    arm_from_tool = np.eye(4)
    arm_from_tool[:3, 3] = [0.05, 0.10, 0.20]
    candidate = build_blade_registration_candidate(
        observation,
        arm_from_tool,
        image_points(),
        registration_config(),
    )
    assert candidate["status"] == "CANDIDATE_REVIEW_REQUIRED"
    assert candidate["motion_usable"] is False
    assert candidate["acting_point_arm_base_m"] == pytest.approx(
        [0.15, 0.20, 0.30]
    )
    assert candidate["acting_point_from_tool_m"] == pytest.approx(
        [0.10, 0.10, 0.10]
    )
    assert candidate["quality_metrics"]["edge_length_mm"] == pytest.approx(
        200.0
    )
    rotation = np.asarray(
        candidate["controlled_frame_rotation_matrix_from_tool"]
    )
    assert rotation.T @ rotation == pytest.approx(np.eye(3))
    assert np.linalg.det(rotation) == pytest.approx(1.0)


def test_registration_keeps_axis_geometry_without_shape_or_forward_cone_gates() -> None:
    observation = {
        "arm_base_points_m": {
            "tip": [-0.07, 0.00, 0.00],
            "heel": [0.00, 0.00, 0.00],
            "spine": [-0.035, 0.15, 0.00],
        },
        "camera_points_m": {
            "tip": [0.00, 0.00, 1.00],
            "heel": [0.07, 0.00, 1.00],
            "spine": [0.035, 0.15, 1.00],
        },
        "depth_diagnostics": {
            name: {"p10_m": 1.0, "p90_m": 1.01}
            for name in ("tip", "heel", "spine")
        },
    }

    candidate = build_blade_registration_candidate(
        observation,
        np.eye(4),
        image_points(),
        registration_config(),
    )

    assert candidate["status"] == "CANDIDATE_REVIEW_REQUIRED"
    assert candidate["quality_metrics"]["edge_length_mm"] == pytest.approx(70.0)
    assert candidate["quality_metrics"]["spine_perpendicular_mm"] == pytest.approx(
        150.0
    )
    assert candidate["quality_metrics"]["tool_forward_axis_cosine"] < 0.0
    assert candidate["quality_metrics"]["edge_length_gate_enabled"] is False
    assert candidate["quality_metrics"]["blade_width_gate_enabled"] is False
    assert candidate["quality_metrics"]["tool_forward_axis_gate_enabled"] is False
    rotation = np.asarray(
        candidate["controlled_frame_rotation_matrix_from_tool"]
    )
    assert rotation.T @ rotation == pytest.approx(np.eye(3))
    assert np.linalg.det(rotation) == pytest.approx(1.0)


def test_registration_rejects_unstable_raw_blade_depth() -> None:
    observation = {
        "arm_base_points_m": {
            "tip": [0.50, -0.09, 0.01],
            "heel": [0.52, -0.09, 0.20],
            "spine": [0.33, -0.09, 0.00],
        },
        "camera_points_m": {
            "tip": [-0.08, 0.11, 1.15],
            "heel": [-0.07, -0.03, 1.04],
            "spine": [-0.08, 0.02, 1.31],
        },
        "depth_diagnostics": {
            "tip": {"p10_m": 1.0, "p90_m": 1.02},
            "heel": {"p10_m": 1.0, "p90_m": 1.02},
            "spine": {"p10_m": 1.0, "p90_m": 1.20},
        },
    }
    candidate = build_blade_registration_candidate(
        observation,
        np.eye(4),
        image_points(),
        registration_config(),
    )
    assert candidate["status"] == "REJECTED_OBSERVATION"
    assert candidate["eligible_for_operator_review"] is False
    assert any(
        "local depth patch" in reason
        for reason in candidate["quality_reasons"]
    )


def test_oblique_blade_uses_bounded_edge_depth_for_reflective_spine() -> None:
    depth_m = np.full((101, 101), 1.55, dtype=np.float32)
    depth_m[47:54, 7:14] = 1.0
    depth_m[47:54, 87:94] = 1.2
    frame = RgbdFrame(
        rgb=np.zeros((101, 101, 3), dtype=np.uint8),
        depth_m=depth_m,
        intrinsics={
            "fx": 100.0,
            "fy": 100.0,
            "cx": 50.0,
            "cy": 50.0,
        },
        timestamp_us=1,
        frame_number=1,
        camera_frame="camera",
        session_epoch="epoch",
        calibration_revision="revision",
        observations={},
    )
    skill = object.__new__(VegetableCuttingSkill)
    skill.config = {
        "tool": {
            "observation_registration": {
                "allow_edge_interpolated_spine_depth": True,
                "spine_depth_interpolation_trigger_mm": 25.0,
                "maximum_spine_depth_correction_mm": 350.0,
            }
        }
    }

    observation = skill._blade_observation(
        frame,
        {
            "tip_yx_1000": [500, 100],
            "heel_yx_1000": [500, 900],
            "spine_yx_1000": [400, 500],
            "confidence": 0.9,
        },
        np.eye(4),
    )

    assert observation["depth_geometry"][
        "tip_to_heel_depth_difference_mm"
    ] == pytest.approx(200.0)
    assert observation["depth_geometry"]["spine_depth_interpolated"] is True
    assert observation["depth_geometry"][
        "spine_correction_exceeds_review_limit"
    ] is True
    assert observation["camera_points_m"]["spine"][2] == pytest.approx(1.1)
    assert observation["depth_diagnostics"]["spine"][
        "used_source"
    ] == "TIP_HEEL_EDGE_INTERPOLATION"


def test_reflective_blade_fallback_uses_image_rays_and_tool_axis() -> None:
    intrinsics = {
        "fx": 1126.78,
        "fy": 1126.11,
        "cx": 951.097,
        "cy": 541.147,
    }
    arm_from_camera = transform_matrix(
        [0.6383491224, 0.5685189707, 0.7320906945],
        [0.2575066239, 0.8840691334, -0.3717635363, -0.1179151340],
    )
    arm_from_tool = np.eye(4, dtype=np.float64)
    arm_from_tool[:3, 3] = [0.44, 0.0, 0.355]
    expected_tool_points = {
        "tip": np.asarray([0.19, 0.025, -0.020]),
        "heel": np.asarray([0.02, 0.025, -0.020]),
        "spine": np.asarray([0.105, 0.025, 0.020]),
        "handle": np.asarray([0.005, 0.025, 0.0]),
    }
    camera_from_arm = np.linalg.inv(arm_from_camera)

    def normalized_point(tool_point: np.ndarray) -> list[int]:
        arm_point = transform_points(arm_from_tool, tool_point)
        camera_point = transform_points(camera_from_arm, arm_point)
        x = (
            intrinsics["fx"] * float(camera_point[0])
            / float(camera_point[2])
            + intrinsics["cx"]
        )
        y = (
            intrinsics["fy"] * float(camera_point[1])
            / float(camera_point[2])
            + intrinsics["cy"]
        )
        return [
            int(round(y * 1000.0 / 1079.0)),
            int(round(x * 1000.0 / 1919.0)),
        ]

    blade = {
        "tip_yx_1000": normalized_point(expected_tool_points["tip"]),
        "heel_yx_1000": normalized_point(expected_tool_points["heel"]),
        "spine_yx_1000": normalized_point(expected_tool_points["spine"]),
        "blade_handle_junction_yx_1000": normalized_point(
            expected_tool_points["heel"]
        ),
        "handle_depth_anchor_yx_1000": normalized_point(
            expected_tool_points["handle"]
        ),
        "confidence": 0.95,
    }
    depth_m = np.full((1080, 1920), 2.5, dtype=np.float32)
    handle_y, handle_x = normalized_yx_to_pixel(
        blade["handle_depth_anchor_yx_1000"],
        (1080, 1920, 3),
    )
    handle_arm = transform_points(
        arm_from_tool,
        expected_tool_points["handle"],
    )
    handle_camera = transform_points(camera_from_arm, handle_arm)
    depth_m[
        handle_y - 3 : handle_y + 4,
        handle_x - 3 : handle_x + 4,
    ] = float(handle_camera[2])
    frame = RgbdFrame(
        rgb=np.zeros((1080, 1920, 3), dtype=np.uint8),
        depth_m=depth_m,
        intrinsics=intrinsics,
        timestamp_us=1,
        frame_number=1,
        camera_frame="camera",
        session_epoch="epoch",
        calibration_revision="revision",
        observations={},
    )
    skill = object.__new__(VegetableCuttingSkill)
    skill.config = load_skill_config()
    raw = skill._blade_observation(
        frame,
        blade,
        arm_from_camera,
    )
    fallback = skill._reflective_blade_observation(
        frame,
        blade,
        arm_from_camera,
        arm_from_tool,
        raw,
    )
    candidate = build_blade_registration_candidate(
        fallback,
        arm_from_tool,
        {
            "tip": blade["tip_yx_1000"],
            "heel": blade["heel_yx_1000"],
            "spine": blade["spine_yx_1000"],
        },
        skill.config["tool"]["observation_registration"],
    )

    assert fallback["depth_geometry"]["reflective_fallback_used"] is True
    assert fallback["depth_diagnostics"]["tip"][
        "raw_reflective_depth_rejected"
    ] is True
    assert candidate["status"] == "CANDIDATE_REVIEW_REQUIRED"
    assert candidate["quality_metrics"]["edge_length_mm"] == pytest.approx(
        170.0,
        abs=8.0,
    )
    assert candidate["quality_metrics"]["tool_forward_axis_cosine"] > 0.9
    assert candidate["acting_point_from_tool_m"] == pytest.approx(
        [0.14, 0.025, -0.020],
        abs=0.020,
    )


def test_consistency_requires_three_recent_quality_passing_observations() -> None:
    report = evaluate_blade_registration_consistency(
        [
            consistency_candidate(0.100),
            consistency_candidate(0.102),
        ],
        consistency_config(),
    )
    assert report["status"] == "MORE_OBSERVATIONS_REQUIRED"
    assert report["motion_usable"] is False

    report = evaluate_blade_registration_consistency(
        [
            consistency_candidate(0.100),
            consistency_candidate(0.102),
            consistency_candidate(0.098),
        ],
        consistency_config(),
    )
    assert report["status"] == "CONSISTENT_REVIEW_REQUIRED"
    assert report["eligible_for_operator_review"] is True
    assert report["quality_metrics"][
        "maximum_acting_point_deviation_mm"
    ] == pytest.approx(2.0)
    assert report[
        "representative_controlled_frame_rpy_from_tool"
    ] == pytest.approx([0.0, 0.0, 0.0])


def test_single_observation_is_immediately_eligible_for_operator_review() -> None:
    config = consistency_config()
    config["required_observations"] = 1

    report = evaluate_blade_registration_consistency(
        [consistency_candidate(0.100)],
        config,
    )

    assert report["status"] == "SINGLE_OBSERVATION_REVIEW_REQUIRED"
    assert report["eligible_for_operator_review"] is True
    assert report["valid_window_observations"] == 1
    assert report[
        "representative_acting_point_from_tool_m"
    ] == pytest.approx([0.100, 0.1, 0.1])


def test_consistency_rejects_bad_or_spatially_unstable_window() -> None:
    rejected = evaluate_blade_registration_consistency(
        [
            consistency_candidate(0.100),
            consistency_candidate(0.101, status="REJECTED_OBSERVATION"),
            consistency_candidate(0.102),
        ],
        consistency_config(),
    )
    assert rejected["status"] == "OBSERVATION_WINDOW_REJECTED"

    unstable = evaluate_blade_registration_consistency(
        [
            consistency_candidate(0.100),
            consistency_candidate(0.101),
            consistency_candidate(0.130),
        ],
        consistency_config(),
    )
    assert unstable["status"] == "INCONSISTENT_OBSERVATIONS"
    assert unstable["eligible_for_operator_review"] is False
