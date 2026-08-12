from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np


PROFILE_SCHEMA = "midbrain.effector_alignment_profile"
PROFILE_SCHEMA_VERSION = 1
MOUNTED_EFFECTOR_SCHEMA = "midbrain.mounted_effector_profile"
MOUNTED_EFFECTOR_SCHEMA_VERSION = 1
ALIGNMENT_EXTENSION_ID = "midbrain.skill.refine_arm_root_translation.v1"
ALIGNMENT_EXTENSION_SCHEMA = "midbrain.effector_visual_alignment"
ALIGNMENT_EXTENSION_SCHEMA_VERSION = 1


def load_effector_profile(path: str | Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(value, dict) and value.get("schema") == MOUNTED_EFFECTOR_SCHEMA:
        value = normalize_mounted_effector_profile(value)
    return validate_effector_profile(value)


def normalize_mounted_effector_profile(value: Any) -> dict[str, Any]:
    """Build the Skill's strict internal view from one mounted-effector profile."""

    if not isinstance(value, dict):
        raise ValueError("mounted effector profile must be an object")
    if value.get("schema") != MOUNTED_EFFECTOR_SCHEMA:
        raise ValueError("mounted effector profile schema is invalid")
    if value.get("schema_version") != MOUNTED_EFFECTOR_SCHEMA_VERSION:
        raise ValueError("mounted effector profile version is unsupported")
    extensions = value.get("extensions")
    if not isinstance(extensions, dict):
        raise ValueError(
            "mounted effector profile has no optional extensions object"
        )
    alignment = extensions.get(ALIGNMENT_EXTENSION_ID)
    if not isinstance(alignment, dict):
        raise ValueError(
            "mounted effector profile does not support arm-root translation "
            "refinement"
        )
    if alignment.get("schema") != ALIGNMENT_EXTENSION_SCHEMA:
        raise ValueError("effector visual-alignment extension schema is invalid")
    if alignment.get("schema_version") != ALIGNMENT_EXTENSION_SCHEMA_VERSION:
        raise ValueError(
            "effector visual-alignment extension version is unsupported"
        )
    compatibility = value.get("robot_compatibility")
    attachment = value.get("kinematic_attachment")
    controlled_frame = value.get("controlled_frame")
    if not isinstance(compatibility, dict):
        raise ValueError("mounted effector robot_compatibility is missing")
    if not isinstance(attachment, dict):
        raise ValueError("mounted effector kinematic_attachment is missing")
    if not isinstance(controlled_frame, dict):
        raise ValueError("mounted effector controlled_frame is missing")
    terminal_transform = _compose_rpy_transforms(
        attachment.get("transform"),
        controlled_frame.get("transform"),
    )
    normalized = {
        "schema": PROFILE_SCHEMA,
        "schema_version": PROFILE_SCHEMA_VERSION,
        "profile_id": value.get("profile_id"),
        "profile_revision": value.get("profile_revision"),
        "display_name": value.get("display_name"),
        "assembly_type": value.get("assembly_type"),
        "qualification_state": alignment.get("qualification_state"),
        "robot_compatibility": {
            "model_id": compatibility.get("model_id"),
            "model_revision": compatibility.get("model_revision"),
            "arm_base_frame": alignment.get("arm_base_frame"),
            "terminal_frame": compatibility.get("terminal_frame"),
            "controlled_frame": controlled_frame.get("frame_id"),
        },
        "kinematic_attachment": {
            "source_schema": MOUNTED_EFFECTOR_SCHEMA,
            "source_reference": (
                f"{value.get('profile_id')}@{value.get('profile_revision')}"
            ),
            "parent_link": attachment.get("parent_frame"),
            "controlled_link": controlled_frame.get("frame_id"),
            "terminal_joint_to_controlled_frame": terminal_transform,
            "qualification": attachment.get("qualification"),
            "replacement_policy": (
                "Changing the selected mounted-effector identity or revision "
                "invalidates this visual-alignment configuration."
            ),
        },
        "capture_motion_policy": copy_json(alignment.get("capture_motion_policy")),
        "refinement_policy": copy_json(alignment.get("refinement_policy")),
        "default_visual_alignment_landmark": alignment.get(
            "default_visual_alignment_landmark"
        ),
        "landmark_fallback_policy": copy_json(
            alignment.get("landmark_fallback_policy")
        ),
        "visual_alignment_landmarks": copy_json(
            alignment.get("visual_alignment_landmarks")
        ),
        "action_frames": copy_json(value.get("acting_frames") or []),
        "invalidation_conditions": list(
            dict.fromkeys(
                [
                    *[str(item) for item in value.get("invalidation_conditions") or []],
                    *[
                        str(item)
                        for item in alignment.get("invalidation_conditions") or []
                    ],
                ]
            )
        ),
    }
    return validate_effector_profile(normalized)


def copy_json(value: Any) -> Any:
    return json.loads(json.dumps(value))


def _compose_rpy_transforms(first: Any, second: Any) -> dict[str, list[float]]:
    first_matrix = _rpy_transform(first, "mounted effector attachment transform")
    second_matrix = _rpy_transform(second, "mounted effector controlled transform")
    composed = first_matrix @ second_matrix
    rotation = composed[:3, :3]
    pitch = math.asin(float(np.clip(-rotation[2, 0], -1.0, 1.0)))
    if abs(math.cos(pitch)) > 1e-9:
        roll = math.atan2(float(rotation[2, 1]), float(rotation[2, 2]))
        yaw = math.atan2(float(rotation[1, 0]), float(rotation[0, 0]))
    else:
        roll = 0.0
        yaw = math.atan2(float(-rotation[0, 1]), float(rotation[1, 1]))
    return {
        "translation_m": composed[:3, 3].tolist(),
        "rpy_rad": [roll, pitch, yaw],
    }


def _rpy_transform(value: Any, label: str) -> np.ndarray:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    translation = np.asarray(value.get("translation_m"), dtype=np.float64)
    rpy = np.asarray(value.get("rpy_rad"), dtype=np.float64)
    if (
        translation.shape != (3,)
        or rpy.shape != (3,)
        or not np.all(np.isfinite(translation))
        or not np.all(np.isfinite(rpy))
    ):
        raise ValueError(f"{label} must contain finite translation and RPY vectors")
    roll, pitch, yaw = [float(item) for item in rpy]
    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    rotation_x = np.asarray([[1, 0, 0], [0, cr, -sr], [0, sr, cr]])
    rotation_y = np.asarray([[cp, 0, sp], [0, 1, 0], [-sp, 0, cp]])
    rotation_z = np.asarray([[cy, -sy, 0], [sy, cy, 0], [0, 0, 1]])
    result = np.eye(4, dtype=np.float64)
    result[:3, :3] = rotation_z @ rotation_y @ rotation_x
    result[:3, 3] = translation
    return result


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
    timestamp_semantics = capture_motion.get(
        "arm_transform_timestamp_semantics",
        "SNAPSHOT_TIME_WITH_FEEDBACK_AGE",
    )
    if timestamp_semantics not in {
        "SNAPSHOT_TIME_WITH_FEEDBACK_AGE",
        "MEASURED_JOINT_BATCH_ACQUISITION_ESTIMATE",
    }:
        raise ValueError(
            "capture_motion_policy arm_transform_timestamp_semantics is invalid"
        )
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
            or len(point_ids) > 8
            or not all(isinstance(item, str) and item for item in point_ids)
            or len(set(point_ids)) != len(point_ids)
        ):
            raise ValueError(
                f"landmark {landmark_id} required_point_ids are invalid"
            )
        if geometry == "SINGLE_REGISTERED_3D_POINT" and len(point_ids) != 1:
            raise ValueError(f"landmark {landmark_id} requires one point")
        if geometry == "MEAN_OF_REGISTERED_3D_POINTS" and len(point_ids) < 2:
            raise ValueError(f"landmark {landmark_id} requires at least two points")
        aggregation = landmark.get("aggregation_policy")
        if not isinstance(aggregation, dict):
            raise ValueError(
                f"landmark {landmark_id} aggregation policy is missing"
            )
        if aggregation.get("method") != (
            "ARITHMETIC_MEAN_OF_ALL_REGISTERED_3D_POINTS"
        ):
            raise ValueError(
                f"landmark {landmark_id} aggregation method is unsupported"
            )
        if aggregation.get("requires_all_points") is not True:
            raise ValueError(
                f"landmark {landmark_id} must require every configured point"
            )
        if aggregation.get("missing_point_policy") != "REJECT_OBSERVATION":
            raise ValueError(
                f"landmark {landmark_id} missing-point policy is unsupported"
            )
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
