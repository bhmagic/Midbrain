from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np


PROFILE_SCHEMA = "midbrain.effector_alignment_profile"
PROFILE_SCHEMA_VERSION = 1


def load_effector_profile(path: str | Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    return validate_effector_profile(value)


def validate_effector_profile(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("effector alignment profile must be an object")
    if value.get("schema") != PROFILE_SCHEMA:
        raise ValueError("effector alignment profile schema is invalid")
    if value.get("schema_version") != PROFILE_SCHEMA_VERSION:
        raise ValueError("effector alignment profile version is unsupported")
    for field in (
        "profile_id",
        "profile_revision",
        "display_name",
        "assembly_type",
        "qualification_state",
        "default_visual_alignment_landmark",
    ):
        if not isinstance(value.get(field), str) or not value[field].strip():
            raise ValueError(f"effector alignment profile {field} is required")
    compatibility = value.get("robot_compatibility")
    if not isinstance(compatibility, dict):
        raise ValueError("robot_compatibility must be an object")
    for field in (
        "model_id",
        "model_revision",
        "arm_base_frame",
        "terminal_frame",
        "controlled_frame",
    ):
        if not isinstance(compatibility.get(field), str) or not compatibility[field]:
            raise ValueError(f"robot_compatibility {field} is required")
    attachment = value.get("kinematic_attachment")
    if not isinstance(attachment, dict):
        raise ValueError("kinematic_attachment must be an object")
    for field in (
        "source_schema",
        "source_reference",
        "parent_link",
        "controlled_link",
        "qualification",
        "replacement_policy",
    ):
        if not isinstance(attachment.get(field), str) or not attachment[field]:
            raise ValueError(f"kinematic_attachment {field} is required")
    attachment_transform = attachment.get("terminal_joint_to_controlled_frame")
    if not isinstance(attachment_transform, dict):
        raise ValueError(
            "kinematic_attachment terminal_joint_to_controlled_frame is required"
        )
    for field in ("translation_m", "rpy_rad"):
        vector = np.asarray(attachment_transform.get(field), dtype=np.float64)
        if vector.shape != (3,) or not np.all(np.isfinite(vector)):
            raise ValueError(
                "kinematic_attachment terminal_joint_to_controlled_frame "
                f"{field} must contain three finite values"
            )
    capture_motion = value.get("capture_motion_policy")
    if not isinstance(capture_motion, dict):
        raise ValueError("capture_motion_policy must be an object")
    for field in (
        "maximum_landmark_motion_m",
        "additional_camera_timing_margin_us",
        "fallback_arm_feedback_age_ms",
        "maximum_arm_feedback_age_ms",
        "preferred_arm_feedback_observation_age_ms",
        "maximum_transform_wait_ms",
        "transform_retry_interval_ms",
        "temporal_sample_count",
    ):
        try:
            number = float(capture_motion[field])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(
                f"capture_motion_policy {field} is required"
            ) from error
        if not np.isfinite(number) or number < 0.0:
            raise ValueError(
                f"capture_motion_policy {field} must be non-negative"
            )
        if field in {
            "maximum_transform_wait_ms",
            "transform_retry_interval_ms",
            "temporal_sample_count",
            "preferred_arm_feedback_observation_age_ms",
        } and number <= 0.0:
            raise ValueError(f"capture_motion_policy {field} must be positive")
    sample_count = capture_motion.get("temporal_sample_count")
    if isinstance(sample_count, bool) or int(sample_count) != float(sample_count):
        raise ValueError(
            "capture_motion_policy temporal_sample_count must be an integer"
        )
    if int(sample_count) < 3 or int(sample_count) > 21:
        raise ValueError(
            "capture_motion_policy temporal_sample_count must be from 3 to 21"
        )
    if (
        float(capture_motion["fallback_arm_feedback_age_ms"])
        > float(capture_motion["maximum_arm_feedback_age_ms"])
    ):
        raise ValueError(
            "capture_motion_policy fallback feedback age exceeds its limit"
        )
    if (
        float(capture_motion["transform_retry_interval_ms"])
        > float(capture_motion["maximum_transform_wait_ms"])
    ):
        raise ValueError(
            "capture_motion_policy retry interval exceeds maximum wait"
        )
    field_path = capture_motion.get("arm_feedback_age_field_path")
    if not isinstance(field_path, list) or not field_path or not all(
        isinstance(item, str) and item.strip() for item in field_path
    ):
        raise ValueError(
            "capture_motion_policy arm_feedback_age_field_path must contain names"
        )
    refinement = value.get("refinement_policy")
    if not isinstance(refinement, dict):
        raise ValueError("refinement_policy must be an object")
    for field in (
        "second_vlm_review_raw_delta_threshold_m",
        "maximum_raw_translation_delta_m",
        "maximum_adopted_translation_delta_m",
        "minimum_landmark_confidence",
        "minimum_same_surface_confidence",
    ):
        try:
            number = float(refinement[field])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"refinement_policy {field} is required") from error
        if not np.isfinite(number):
            raise ValueError(f"refinement_policy {field} must be finite")
        if field.startswith("minimum_") and not 0.0 <= number <= 1.0:
            raise ValueError(
                f"refinement_policy {field} must be from zero to one"
            )
        if field == "second_vlm_review_raw_delta_threshold_m" and number < 0.0:
            raise ValueError(
                "refinement_policy review threshold must be non-negative"
            )
        if field.startswith("maximum_") and number <= 0.0:
            raise ValueError(
                f"refinement_policy {field} must be positive"
            )
    if (
        float(refinement["second_vlm_review_raw_delta_threshold_m"])
        > float(refinement["maximum_raw_translation_delta_m"])
    ):
        raise ValueError(
            "refinement_policy review threshold must not exceed the raw-delta limit"
        )
    landmarks = value.get("visual_alignment_landmarks")
    if not isinstance(landmarks, list) or not landmarks:
        raise ValueError("visual_alignment_landmarks must be a non-empty array")
    observed_ids: set[str] = set()
    normalized_landmarks: list[dict[str, Any]] = []
    for landmark in landmarks:
        if not isinstance(landmark, dict):
            raise ValueError("each visual alignment landmark must be an object")
        landmark_id = str(landmark.get("landmark_id") or "")
        if not landmark_id or landmark_id in observed_ids:
            raise ValueError("visual alignment landmark IDs must be unique")
        geometry = str(landmark.get("geometry") or "")
        if geometry not in {
            "SINGLE_REGISTERED_3D_POINT",
            "MEAN_OF_REGISTERED_3D_POINTS",
        }:
            raise ValueError(f"landmark {landmark_id} geometry is unsupported")
        point_ids = landmark.get("required_point_ids")
        if (
            not isinstance(point_ids, list)
            or not point_ids
            or not all(isinstance(item, str) and item for item in point_ids)
            or len(set(point_ids)) != len(point_ids)
        ):
            raise ValueError(
                f"landmark {landmark_id} required_point_ids are invalid"
            )
        if geometry == "SINGLE_REGISTERED_3D_POINT" and len(point_ids) != 1:
            raise ValueError(f"landmark {landmark_id} requires one point")
        if geometry == "MEAN_OF_REGISTERED_3D_POINTS" and len(point_ids) < 2:
            raise ValueError(f"landmark {landmark_id} requires paired points")
        description = landmark.get("description_for_vlm")
        if not isinstance(description, str) or len(description.strip()) < 20:
            raise ValueError(f"landmark {landmark_id} VLM description is missing")
        binding = landmark.get("tool_point_binding")
        if not isinstance(binding, dict):
            raise ValueError(f"landmark {landmark_id} tool binding is missing")
        for field in ("source", "qualification"):
            if not isinstance(binding.get(field), str) or not binding[field].strip():
                raise ValueError(
                    f"landmark {landmark_id} tool binding {field} is missing"
                )
        explicit_point = binding.get(
            "controlled_frame_to_landmark_translation_m"
        )
        legacy_point = binding.get("translation_m")
        runtime_key = str(binding.get("runtime_key") or "").strip()
        if (
            explicit_point is not None
            and "landmark_to_controlled_frame_translation_m" not in binding
        ):
            raise ValueError(
                f"landmark {landmark_id} explicit binding requires its inverse"
            )
        if explicit_point is None and legacy_point is None:
            if not runtime_key:
                raise ValueError(
                    f"landmark {landmark_id} has no static or runtime point binding"
                )
        else:
            try:
                resolve_tool_landmark_point(landmark)
            except RuntimeError as error:
                raise ValueError(
                    f"landmark {landmark_id} tool binding is invalid: {error}"
                ) from error
        normalized_landmarks.append(json.loads(json.dumps(landmark)))
        observed_ids.add(landmark_id)
    default_id = str(value["default_visual_alignment_landmark"])
    if default_id not in observed_ids:
        raise ValueError("default visual alignment landmark is unavailable")
    fallback = value.get("landmark_fallback_policy")
    if not isinstance(fallback, dict):
        raise ValueError("landmark_fallback_policy must be an object")
    selection_order = fallback.get("selection_order")
    if (
        not isinstance(selection_order, list)
        or not selection_order
        or not all(isinstance(item, str) for item in selection_order)
        or len(set(selection_order)) != len(selection_order)
        or any(item not in observed_ids for item in selection_order)
    ):
        raise ValueError("landmark fallback selection_order is invalid")
    if fallback.get("automatic_substitution_allowed") is not False:
        raise ValueError(
            "unqualified automatic visual-landmark substitution is not allowed"
        )
    normalized = json.loads(json.dumps(value))
    normalized["visual_alignment_landmarks"] = normalized_landmarks
    return normalized


def select_visual_landmark(
    profile: dict[str, Any],
    landmark_id: str | None = None,
) -> dict[str, Any]:
    normalized = validate_effector_profile(profile)
    requested_id = "" if landmark_id is None else str(landmark_id).strip()
    profile_ids = {
        str(landmark["landmark_id"])
        for landmark in normalized["visual_alignment_landmarks"]
    }
    default_sentinels = {
        "auto",
        "automatic",
        "default",
        "profile-default",
        "profile_default",
    }
    if requested_id in profile_ids:
        selected_id = requested_id
    elif requested_id.casefold() in default_sentinels:
        selected_id = str(normalized["default_visual_alignment_landmark"])
    else:
        selected_id = requested_id or str(
            normalized["default_visual_alignment_landmark"]
        )
    for landmark in normalized["visual_alignment_landmarks"]:
        if landmark["landmark_id"] == selected_id:
            return landmark
    raise ValueError(f"landmark {selected_id!r} is not in the effector profile")


def resolve_tool_landmark_point(
    landmark: dict[str, Any],
    *,
    runtime_bindings: dict[str, Any] | None = None,
) -> np.ndarray:
    binding = landmark.get("tool_point_binding")
    if not isinstance(binding, dict):
        raise ValueError("landmark tool_point_binding is missing")
    value = binding.get("controlled_frame_to_landmark_translation_m")
    legacy_value = binding.get("translation_m")
    if value is None:
        value = legacy_value
    elif legacy_value is not None:
        legacy_point = np.asarray(legacy_value, dtype=np.float64)
        explicit_point = np.asarray(value, dtype=np.float64)
        if (
            legacy_point.shape != (3,)
            or explicit_point.shape != (3,)
            or not np.allclose(legacy_point, explicit_point, atol=1e-12)
        ):
            raise RuntimeError(
                "legacy and explicit landmark point bindings disagree"
            )
    runtime_key = str(binding.get("runtime_key") or "")
    if value is None and runtime_key and isinstance(runtime_bindings, dict):
        value = runtime_bindings.get(runtime_key)
    point = np.asarray(value, dtype=np.float64)
    if point.shape != (3,) or not np.all(np.isfinite(point)):
        raise RuntimeError(
            f"landmark {landmark.get('landmark_id')} has no qualified "
            "tool-frame point binding"
        )
    inverse_value = binding.get(
        "landmark_to_controlled_frame_translation_m"
    )
    if inverse_value is not None:
        inverse = np.asarray(inverse_value, dtype=np.float64)
        if (
            inverse.shape != (3,)
            or not np.all(np.isfinite(inverse))
            or not np.allclose(inverse, -point, atol=1e-12)
        ):
            raise RuntimeError(
                "landmark-to-controlled-frame binding is not the inverse of "
                "the controlled-frame landmark point"
            )
    return point
