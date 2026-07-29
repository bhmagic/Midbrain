from __future__ import annotations

from typing import Any

import numpy as np


def _unit(vector: np.ndarray, label: str) -> np.ndarray:
    value = np.asarray(vector, dtype=np.float64)
    norm = float(np.linalg.norm(value))
    if value.shape != (3,) or not np.isfinite(norm) or norm <= 1e-9:
        raise ValueError(f"{label} must be a finite nonzero 3-vector")
    return value / norm


def _rotate_about_axis(
    vector: np.ndarray,
    axis: np.ndarray,
    angle_rad: float,
) -> np.ndarray:
    direction = _unit(axis, "rotation axis")
    value = np.asarray(vector, dtype=np.float64)
    cosine = float(np.cos(angle_rad))
    sine = float(np.sin(angle_rad))
    return (
        value * cosine
        + np.cross(direction, value) * sine
        + direction * float(direction @ value) * (1.0 - cosine)
    )


def _rotation_matrix_to_quaternion_xyzw(rotation: np.ndarray) -> list[float]:
    matrix = np.asarray(rotation, dtype=np.float64)
    if matrix.shape != (3, 3) or not np.all(np.isfinite(matrix)):
        raise ValueError("rotation must be a finite 3x3 matrix")
    trace = float(np.trace(matrix))
    if trace > 0.0:
        scale = np.sqrt(trace + 1.0) * 2.0
        w = 0.25 * scale
        x = (matrix[2, 1] - matrix[1, 2]) / scale
        y = (matrix[0, 2] - matrix[2, 0]) / scale
        z = (matrix[1, 0] - matrix[0, 1]) / scale
    else:
        index = int(np.argmax(np.diag(matrix)))
        if index == 0:
            scale = np.sqrt(1.0 + matrix[0, 0] - matrix[1, 1] - matrix[2, 2]) * 2.0
            w = (matrix[2, 1] - matrix[1, 2]) / scale
            x = 0.25 * scale
            y = (matrix[0, 1] + matrix[1, 0]) / scale
            z = (matrix[0, 2] + matrix[2, 0]) / scale
        elif index == 1:
            scale = np.sqrt(1.0 + matrix[1, 1] - matrix[0, 0] - matrix[2, 2]) * 2.0
            w = (matrix[0, 2] - matrix[2, 0]) / scale
            x = (matrix[0, 1] + matrix[1, 0]) / scale
            y = 0.25 * scale
            z = (matrix[1, 2] + matrix[2, 1]) / scale
        else:
            scale = np.sqrt(1.0 + matrix[2, 2] - matrix[0, 0] - matrix[1, 1]) * 2.0
            w = (matrix[1, 0] - matrix[0, 1]) / scale
            x = (matrix[0, 2] + matrix[2, 0]) / scale
            y = (matrix[1, 2] + matrix[2, 1]) / scale
            z = 0.25 * scale
    quaternion = np.asarray([x, y, z, w], dtype=np.float64)
    quaternion /= np.linalg.norm(quaternion)
    return quaternion.tolist()


def desired_blade_frame(
    entry_arm_base_m: list[float],
    exit_arm_base_m: list[float],
    board_normal_arm_base: np.ndarray,
    blade_yaw_deg: float,
) -> dict[str, Any]:
    """Describe the desired blade frame without converting it to a tool pose."""
    up = _unit(board_normal_arm_base, "board normal")
    cut_direction = _unit(
        np.asarray(exit_arm_base_m, dtype=np.float64)
        - np.asarray(entry_arm_base_m, dtype=np.float64),
        "cut direction",
    )
    yaw_axis = np.asarray([0.0, 0.0, 1.0], dtype=np.float64)
    edge = _rotate_about_axis(
        cut_direction,
        yaw_axis,
        np.deg2rad(float(blade_yaw_deg)),
    )
    edge -= up * float(edge @ up)
    edge = _unit(edge, "yaw-adjusted blade edge")
    down = -up
    normal = _unit(np.cross(down, edge), "blade normal")
    rotation = np.column_stack([edge, normal, down])
    return {
        "semantics": "DESIRED_BLADE_FRAME_NOT_TOOL_FRAME",
        "edge_axis_arm_base": edge.tolist(),
        "normal_axis_arm_base": normal.tolist(),
        "down_axis_arm_base": down.tolist(),
        "rotation_matrix_arm_base": rotation.tolist(),
        "rotation_xyzw_arm_base": _rotation_matrix_to_quaternion_xyzw(rotation),
        "yaw_deg": float(blade_yaw_deg),
        "yaw_axis": "ARM_BASE_POSITIVE_Z",
    }


def build_execution_preview(
    cuts: list[dict[str, Any]],
    board_normal_arm_base: np.ndarray,
    *,
    vegetable_maximum_height_mm: float,
    blade_yaw_deg: float,
    handoff: dict[str, Any],
    execution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an inspectable sequence that requires calibration and takeover."""
    if not cuts:
        raise ValueError("execution preview requires at least one cut")
    up = _unit(board_normal_arm_base, "board normal")
    approach_offset_mm = max(
        float(handoff["minimum_approach_board_offset_mm"]),
        float(vegetable_maximum_height_mm)
        + float(handoff["approach_clearance_above_vegetable_mm"]),
    )
    approach_offset_m = approach_offset_mm / 1000.0
    execution_config = execution or {}
    post_cut_retract_m = float(
        execution_config.get("post_cut_retract_m", 0.1)
    )
    retract_kp_multiplier = float(
        execution_config.get(
            "retract_kp_multiplier",
            handoff["mit_kp_multiplier"],
        )
    )
    segments: list[dict[str, Any]] = []

    for index, cut in enumerate(cuts):
        center = np.asarray(cut["center_arm_base_m"], dtype=np.float64)
        contact = center
        approach = center + up * approach_offset_m
        blade_frame = desired_blade_frame(
            cut["entry_arm_base_m"],
            cut["exit_arm_base_m"],
            up,
            blade_yaw_deg,
        )
        pose = {
            "coordinate_frame": "rebot_arm_base",
            "position_m": approach.tolist(),
            "blade_frame": blade_frame,
            "tool_pose_available": False,
        }
        if index == 0:
            segments.extend(
                [
                    {
                        "action": "TRANSFER_TO_FIRST_APPROACH",
                        "cut_index": index,
                        "backend": "PRESS_MIT",
                        "target": pose,
                        "requested_transfer_speed_m_s": float(
                            handoff["requested_transfer_speed_m_s"]
                        ),
                        "speed_validated": bool(
                            handoff["transfer_speed_validated"]
                        ),
                    },
                    {
                        "action": "CLEARANCE_HEIGHT_FIRST_CUT_VISUAL_PREALIGNMENT",
                        "cut_index": index,
                        "backend": "VLM_RGBD_RELATIVE_TRANSLATION",
                        "vlm_output": "OBSERVED_BLADE_CONTROLLED_POINT_PIXEL",
                        "absolute_camera_translation_used_for_correction": False,
                        "human_confirmation_required": False,
                    },
                    {
                        "action": "FIRST_CUT_HUMAN_REVIEW_OPTIONAL_VLM_RELATIVE",
                        "cut_index": index,
                        "backend": "HUMAN_GATE",
                        "expected_output": "FIRST_CUT_ALIGNMENT_CONTRACT_V1",
                        "correction_semantics": (
                            "BOUNDED_CAMERA_PIXEL_SERVO_TO_ARM_BASE_TRANSLATION"
                        ),
                        "vlm_called_only_after_human_rejection": False,
                        "automatic_clearance_prealignment_vlm_calls": 1,
                        "additional_vlm_calls_require_human_rejection": True,
                        "maximum_vlm_calls": None,
                        "operator_requested_vlm_rounds_unbounded": True,
                        "motion_usable": False,
                        "human_confirmation_required": bool(
                            handoff["first_cut_human_confirmation_required"]
                        ),
                        "operator_choices": [
                            "YES",
                            "NO_READJUST",
                            "FULL_STOP_GO_HOME",
                        ],
                    },
                ]
            )
        else:
            segments.append(
                {
                    "action": "MIT_SHIFT_TO_NEXT_APPROACH",
                    "cut_index": index,
                    "backend": "PRESS_MIT",
                    "target": pose,
                }
            )
        segments.extend(
            [
                {
                    "action": "MIT_CUT_STROKE",
                    "cut_index": index,
                    "backend": "PRESS_MIT",
                    "start_position_m": approach.tolist(),
                    "target_position_m": contact.tolist(),
                    "blade_frame": blade_frame,
                    "duration_s": float(handoff["cut_duration_s"]),
                    "kp_multiplier": float(handoff["mit_kp_multiplier"]),
                    "target_board_offset_mm": float(
                        handoff["cut_target_board_offset_mm"]
                    ),
                    "below_board_target_allowed": bool(
                        handoff["allow_below_board_target"]
                    ),
                },
                {
                    "action": "MIT_RETRACT",
                    "cut_index": index,
                    "backend": "PRESS_MIT",
                    "distance_from_board_m": post_cut_retract_m,
                    "kp_multiplier": retract_kp_multiplier,
                    "purpose": "BLADE_UNSTICK_AND_CLEARANCE",
                },
                {
                    "action": "NO_COORDINATE_RECHECK_AFTER_HUMAN_APPROVAL",
                    "cut_index": index,
                    "backend": "HUMAN_APPROVED_SEQUENCE",
                    "vlm_called_by_default": False,
                    "coordinate_recheck_required": False,
                },
            ]
        )

    for sequence, segment in enumerate(segments):
        segment["sequence"] = sequence
        segment["motion_submitted"] = False
    return {
        "status": "REVIEW_REQUIRED_BEFORE_SUBMISSION",
        "motion_submission_enabled": False,
        "motion_submission_available_after_takeover": True,
        "motion_submitted": False,
        "pose_semantics": "FIXED_HARD_MOUNT_CONTROLLED_FRAME",
        "approach_board_offset_mm": approach_offset_mm,
        "vegetable_maximum_height_mm": float(vegetable_maximum_height_mm),
        "segments": segments,
        "completion_protocol": [
            "prompt the operator to physically detach and remove the knife",
            "record the operator tool-removal confirmation",
            "start Integrated safe termination only after human confirmation",
            "verify safe-home and Midbrain hardware release",
        ],
        "abort_protocol": {
            "always_available": True,
            "required_physical_action": "INTEGRATED_FLOAT",
            "skill_can_issue_action": True,
            "hard_stop_remains_external": True,
        },
    }
