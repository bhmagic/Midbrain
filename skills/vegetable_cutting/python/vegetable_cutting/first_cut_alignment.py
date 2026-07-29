from __future__ import annotations

from typing import Any

import numpy as np

from .math3d import deproject_pixel, normalized_yx_to_pixel


def build_first_cut_pixel_servo_measurement(
    observation: dict[str, Any],
    *,
    image_shape: tuple[int, ...],
    intrinsics: dict[str, Any],
    target_camera_m: list[float] | np.ndarray,
    no_correction_tolerance_mm: float,
) -> dict[str, Any]:
    target = np.asarray(target_camera_m, dtype=np.float64)
    if target.shape != (3,) or not np.all(np.isfinite(target)):
        raise ValueError("first-cut camera target must be a finite 3-vector")
    if float(target[2]) <= 0.0:
        raise ValueError("first-cut camera target must be in front of the camera")

    blade_yx = normalized_yx_to_pixel(
        observation["blade_controlled_point_yx_1000"],
        image_shape,
    )
    blade_at_target_depth = deproject_pixel(
        blade_yx,
        float(target[2]),
        intrinsics,
    )
    translation_camera_m = target - blade_at_target_depth
    translation_magnitude_mm = float(
        np.linalg.norm(translation_camera_m) * 1000.0
    )

    target_x = (
        float(intrinsics["fx"]) * float(target[0]) / float(target[2])
        + float(intrinsics["cx"])
    )
    target_y = (
        float(intrinsics["fy"]) * float(target[1]) / float(target[2])
        + float(intrinsics["cy"])
    )
    target_yx = [
        int(round(target_y)),
        int(round(target_x)),
    ]
    pixel_residual_yx = [
        float(target_y - blade_yx[0]),
        float(target_x - blade_yx[1]),
    ]
    meaningful_without_correction = bool(
        translation_magnitude_mm <= float(no_correction_tolerance_mm)
    )
    return {
        "translation_offset_camera_mm": dict(
            zip(
                ("x", "y", "z"),
                (translation_camera_m * 1000.0).tolist(),
                strict=True,
            )
        ),
        "translation_magnitude_mm": translation_magnitude_mm,
        "blade_controlled_point_pixel_yx": list(blade_yx),
        "target_controlled_point_pixel_yx": target_yx,
        "pixel_residual_yx": pixel_residual_yx,
        "target_camera_m": target.tolist(),
        "blade_assumed_camera_m": blade_at_target_depth.tolist(),
        "blade_depth_semantics": (
            "TARGET_REVIEW_DEPTH_FOR_REFLECTIVE_BLADE_IMAGE_PLANE_SERVO"
        ),
        "meaningful_without_correction": meaningful_without_correction,
    }


def build_first_cut_alignment_contract(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "HUMAN_REVIEW_THEN_BOUNDED_VLM_VISUAL_SERVO",
        "motion_usable": False,
        "motion_submission_enabled": False,
        "correction_semantics": (
            "BOUNDED_CAMERA_PIXEL_SERVO_TO_ARM_BASE_TRANSLATION"
        ),
        "vlm_output_semantics": "OBSERVED_BLADE_CONTROLLED_POINT_PIXEL",
        "minimum_vlm_confidence": float(config["minimum_vlm_confidence"]),
        "no_correction_tolerance_mm": float(
            config["no_correction_tolerance_mm"]
        ),
        "no_correction_rotation_tolerance_deg": float(
            config["no_correction_rotation_tolerance_deg"]
        ),
        "maximum_translation_mm": float(config["maximum_translation_mm"]),
        "maximum_rotation_deg": float(config["maximum_rotation_deg"]),
        "operator_confirmation_required": bool(
            config["operator_confirmation_required"]
        ),
        "loop_semantics": (
            "CAPTURE_CORRECT_MOVE_RECAPTURE_FIRST_CUT_ONLY"
        ),
        "operator_choices": [
            "YES",
            "NO_READJUST",
            "FULL_STOP_GO_HOME",
        ],
    }


def build_first_cut_alignment_correction(
    observation: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    reasons: list[str] = []
    advisories: list[str] = []
    confidence = float(observation["confidence"])
    if bool(observation["person_or_animal_visible_in_workspace"]):
        reasons.append("a person or animal is visible in the robot workspace")
    if not bool(observation["blade_and_target_visible"]):
        reasons.append("the blade acting point and target are not both visible")
    if not bool(observation["depth_evidence_used"]):
        reasons.append("registered depth evidence was not used")
    if not bool(observation["orange_cut_target_matches_vegetable"]):
        advisories.append(
            "VLM did not confirm the board-plane first-cut target against the "
            "vegetable; the human first-cut review remains authoritative"
        )
    if confidence < float(config["minimum_vlm_confidence"]):
        reasons.append("first-cut alignment confidence is below the configured limit")

    translation_payload = observation["translation_offset_arm_base_mm"]
    rotation_payload = observation["rotation_offset_arm_base_deg"]
    translation_mm = np.asarray(
        [
            translation_payload["x"],
            translation_payload["y"],
            translation_payload["z"],
        ],
        dtype=np.float64,
    )
    rotation_deg = np.asarray(
        [
            rotation_payload["roll"],
            rotation_payload["pitch"],
            rotation_payload["yaw"],
        ],
        dtype=np.float64,
    )
    if not np.all(np.isfinite(translation_mm)) or not np.all(
        np.isfinite(rotation_deg)
    ):
        reasons.append("the 6D correction contains non-finite values")
    translation_magnitude_mm = float(np.linalg.norm(translation_mm))
    rotation_magnitude_deg = float(np.linalg.norm(rotation_deg))
    if translation_magnitude_mm > float(config["maximum_translation_mm"]):
        reasons.append("suggested first-cut translation exceeds the configured limit")
    if np.max(np.abs(rotation_deg)) > float(config["maximum_rotation_deg"]):
        reasons.append("suggested first-cut rotation exceeds the configured limit")

    meaningful_without_correction = bool(
        observation["meaningful_without_correction"]
    )
    translation_small = translation_magnitude_mm <= float(
        config["no_correction_tolerance_mm"]
    )
    rotation_small = rotation_magnitude_deg <= float(
        config["no_correction_rotation_tolerance_deg"]
    )
    if meaningful_without_correction and not (
        translation_small and rotation_small
    ):
        reasons.append(
            "VLM meaningful-placement flag conflicts with its nontrivial 6D offset"
        )

    if reasons:
        status = "REJECTED_OBSERVATION"
    elif meaningful_without_correction or (
        translation_small and rotation_small
    ):
        status = "NO_CORRECTION_REVIEW_REQUIRED"
    else:
        status = "CORRECTION_REVIEW_REQUIRED"
    return {
        "status": status,
        "motion_usable": False,
        "motion_submission_enabled": False,
        "operator_confirmation_required": True,
        "coordinate_frame": "rebot_arm_base",
        "correction_semantics": (
            "BOUNDED_CAMERA_PIXEL_SERVO_TO_ARM_BASE_TRANSLATION"
        ),
        "translation_arm_base_m": (translation_mm / 1000.0).tolist(),
        "rotation_offset_rpy_rad": np.deg2rad(rotation_deg).tolist(),
        "translation_components_mm": {
            "arm_base_x": float(translation_mm[0]),
            "arm_base_y": float(translation_mm[1]),
            "arm_base_z": float(translation_mm[2]),
        },
        "rotation_components_deg": {
            "roll_x": float(rotation_deg[0]),
            "pitch_y": float(rotation_deg[1]),
            "yaw_z": float(rotation_deg[2]),
        },
        "translation_magnitude_mm": translation_magnitude_mm,
        "rotation_magnitude_deg": rotation_magnitude_deg,
        "confidence": confidence,
        "meaningful_without_correction": meaningful_without_correction,
        "depth_evidence_used": bool(observation["depth_evidence_used"]),
        "image_plane_alignment_meaningful": bool(
            observation["image_plane_alignment_meaningful"]
        ),
        "depth_alignment_meaningful": bool(
            observation["depth_alignment_meaningful"]
        ),
        "orange_cut_target_matches_vegetable": bool(
            observation["orange_cut_target_matches_vegetable"]
        ),
        "quality_reasons": reasons,
        "advisories": advisories,
        "operator_choices": [
            "YES",
            "NO_READJUST",
            "FULL_STOP_GO_HOME",
        ],
        "notes": str(observation.get("notes") or ""),
    }
