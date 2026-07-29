from __future__ import annotations

import hashlib
import hmac
import json
import math
import time
from pathlib import Path
from typing import Any

import numpy as np

from .foundation_engine import (
    PROVIDER_COMPATIBILITY_ROUTE,
    SKILL_LOCAL_ROUTE,
)
from .math3d import transform_from_payload


ROUTE_RUN_SCHEMA = (
    "midbrain.skill.stationary_world_arm_alignment.route_run"
)
ROUTE_COMPARISON_SCHEMA = (
    "midbrain.skill.stationary_world_arm_alignment.route_comparison"
)
ROUTE_RECORD_VERSION = 1
REQUIRED_OBSERVATION_FIELDS = (
    "capture_id",
    "rgb_sha256",
    "registered_depth_sha256",
    "camera_route_id",
    "camera_calibration_revision",
    "camera_boot_id",
    "vio_session_epoch",
    "frame_timestamp_us",
    "gripper_configuration",
)
ROUTES = {
    PROVIDER_COMPATIBILITY_ROUTE,
    SKILL_LOCAL_ROUTE,
}
DEFAULT_THRESHOLDS = {
    "maximum_translation_delta_m": 0.03,
    "maximum_rotation_delta_rad": math.radians(5.0),
    "maximum_repeatability_translation_span_m": 0.02,
    "maximum_repeatability_rotation_span_rad": math.radians(3.0),
}
REPLAY_BUNDLE_SCHEMA = "physical_agent.phase5_replay_bundle"


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def observation_fingerprint(observation: dict[str, Any]) -> str:
    if not isinstance(observation, dict):
        raise TypeError("route comparison observation must be an object")
    missing = [
        field
        for field in REQUIRED_OBSERVATION_FIELDS
        if observation.get(field) in (None, "")
    ]
    if missing:
        raise ValueError(
            "route comparison observation is missing: " + ", ".join(missing)
        )
    return hashlib.sha256(_canonical_bytes(observation)).hexdigest()


def observation_from_replay_bundle(
    manifest: dict[str, Any],
    *,
    bundle_root: Path,
    gripper_configuration: str,
    verify_payloads: bool = True,
) -> dict[str, Any]:
    """Build one immutable route input identity from a captured replay bundle."""
    if manifest.get("schema") != REPLAY_BUNDLE_SCHEMA:
        raise ValueError("unsupported replay-bundle schema")
    if int(manifest.get("schema_version") or 0) != 1:
        raise ValueError("unsupported replay-bundle schema version")
    capture_id = str(manifest.get("bundle_id") or "").strip()
    if not capture_id:
        raise ValueError("replay bundle is missing bundle_id")
    normalized_gripper = str(gripper_configuration).strip().upper()
    if normalized_gripper not in {"INSTALLED", "DETACHED"}:
        raise ValueError("gripper configuration must be INSTALLED or DETACHED")

    payloads = manifest.get("payloads")
    if not isinstance(payloads, dict):
        raise ValueError("replay bundle is missing payload records")
    rgb = _replay_payload(payloads, "rgb", bundle_root, verify_payloads)
    depth = _replay_payload(
        payloads,
        "registered_depth",
        bundle_root,
        verify_payloads,
    )
    records = manifest.get("records")
    fabric = records.get("fabric") if isinstance(records, dict) else None
    if not isinstance(fabric, dict):
        raise ValueError("replay bundle is missing Fabric provenance")
    route_observation = fabric.get("route_observation")
    bundle_observation = fabric.get("rgbd_bundle_observation")
    optional_streams = fabric.get("optional_streams")
    if not isinstance(route_observation, dict) or not isinstance(
        bundle_observation,
        dict,
    ):
        raise ValueError("replay bundle is missing RGB-D route or bundle provenance")
    route_data = route_observation.get("data")
    camera_route_id = (
        route_data.get("preferred_route_id")
        if isinstance(route_data, dict)
        else None
    )
    body_pose = (
        optional_streams.get("localization.body.pose")
        if isinstance(optional_streams, dict)
        else None
    )
    body_data = body_pose.get("data") if isinstance(body_pose, dict) else None
    vio_session_epoch = (
        body_data.get("session_epoch")
        if isinstance(body_data, dict)
        else None
    )
    observation = {
        "capture_id": capture_id,
        "rgb_sha256": rgb["sha256"],
        "registered_depth_sha256": depth["sha256"],
        "camera_route_id": camera_route_id,
        "camera_calibration_revision": bundle_observation.get(
            "calibration_revision"
        ),
        "camera_boot_id": bundle_observation.get("boot_id"),
        "vio_session_epoch": vio_session_epoch,
        "frame_timestamp_us": bundle_observation.get("observed_at_us"),
        "gripper_configuration": normalized_gripper,
        "camera_provider_id": bundle_observation.get("provider_id"),
        "camera_provider_instance_id": bundle_observation.get(
            "provider_instance_id"
        ),
        "replay_bundle_schema": manifest.get("schema"),
        "replay_bundle_schema_version": manifest.get("schema_version"),
        "payloads_verified": bool(verify_payloads),
        "source_payloads": {
            "rgb": rgb["path"],
            "registered_depth": depth["path"],
        },
    }
    observation_fingerprint(observation)
    return observation


def load_replay_observation(
    manifest_path: Path,
    *,
    gripper_configuration: str,
    verify_payloads: bool = True,
) -> dict[str, Any]:
    resolved = manifest_path.resolve()
    manifest = json.loads(resolved.read_text(encoding="utf-8"))
    return observation_from_replay_bundle(
        manifest,
        bundle_root=resolved.parent,
        gripper_configuration=gripper_configuration,
        verify_payloads=verify_payloads,
    )


def _replay_payload(
    payloads: dict[str, Any],
    label: str,
    bundle_root: Path,
    verify_payloads: bool,
) -> dict[str, str]:
    record = payloads.get(label)
    if not isinstance(record, dict):
        raise ValueError(f"replay bundle is missing {label} payload")
    relative_path = str(record.get("path") or "").replace("\\", "/")
    expected_sha256 = str(record.get("sha256") or "").lower()
    if not re_full_sha256(expected_sha256):
        raise ValueError(f"replay bundle {label} SHA-256 is invalid")
    path = (bundle_root / relative_path).resolve()
    try:
        path.relative_to(bundle_root.resolve())
    except ValueError as error:
        raise ValueError(
            f"replay bundle {label} payload escapes the bundle root"
        ) from error
    if verify_payloads:
        if not path.is_file():
            raise ValueError(f"replay bundle {label} payload does not exist")
        actual_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        if not hmac.compare_digest(actual_sha256, expected_sha256):
            raise ValueError(
                f"replay bundle {label} payload SHA-256 does not match"
            )
    return {
        "path": path.relative_to(bundle_root.resolve()).as_posix(),
        "sha256": expected_sha256,
    }


def re_full_sha256(value: str) -> bool:
    return len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _rotation_delta_rad(left: np.ndarray, right: np.ndarray) -> float:
    relative = left[:3, :3].T @ right[:3, :3]
    cosine = float(np.clip((np.trace(relative) - 1.0) / 2.0, -1.0, 1.0))
    return float(math.acos(cosine))


def _repeatability_metrics(
    transforms: list[dict[str, Any]],
) -> dict[str, Any]:
    if not transforms:
        return {
            "sample_count": 0,
            "maximum_translation_span_m": None,
            "maximum_rotation_span_rad": None,
        }
    matrices = [transform_from_payload(value) for value in transforms]
    maximum_translation = 0.0
    maximum_rotation = 0.0
    for index, left in enumerate(matrices):
        for right in matrices[index + 1 :]:
            maximum_translation = max(
                maximum_translation,
                float(np.linalg.norm(left[:3, 3] - right[:3, 3])),
            )
            maximum_rotation = max(
                maximum_rotation,
                _rotation_delta_rad(left, right),
            )
    return {
        "sample_count": len(matrices),
        "maximum_translation_span_m": maximum_translation,
        "maximum_rotation_span_rad": maximum_rotation,
    }


def build_route_run_record(
    *,
    case_id: str,
    route: str,
    observation: dict[str, Any],
    status: str,
    latency_ms: float,
    world_from_base: dict[str, Any] | None,
    sample_transforms: list[dict[str, Any]],
    lifecycle: dict[str, Any],
    operator_effort: dict[str, Any],
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_route = str(route).strip().upper()
    if normalized_route not in ROUTES:
        raise ValueError(f"unsupported comparison route: {normalized_route}")
    normalized_status = str(status).strip().upper()
    if normalized_status not in {"SUCCEEDED", "FAILED", "REJECTED"}:
        raise ValueError(f"unsupported route-run status: {normalized_status}")
    if float(latency_ms) < 0 or not math.isfinite(float(latency_ms)):
        raise ValueError("route-run latency must be finite and nonnegative")
    if normalized_status == "SUCCEEDED" and world_from_base is None:
        raise ValueError("successful route run requires world_from_base")
    if normalized_status == "SUCCEEDED":
        transform_from_payload(world_from_base)
    if normalized_status != "SUCCEEDED" and not isinstance(error, dict):
        raise ValueError("failed or rejected route run requires structured error")

    observation_copy = json.loads(_canonical_bytes(observation))
    fingerprint = observation_fingerprint(observation_copy)
    safety = {
        "physical_action_submitted": False,
        "controller_called": False,
        "control_mode_changed": False,
    }
    return {
        "schema": ROUTE_RUN_SCHEMA,
        "schema_version": ROUTE_RECORD_VERSION,
        "case_id": str(case_id),
        "route": normalized_route,
        "created_at_us": time.time_ns() // 1000,
        "observation": observation_copy,
        "observation_fingerprint": fingerprint,
        "outcome": {
            "status": normalized_status,
            "latency_ms": float(latency_ms),
            "world_from_base": world_from_base,
            "error": error,
        },
        "repeatability": _repeatability_metrics(sample_transforms),
        "lifecycle": json.loads(_canonical_bytes(lifecycle)),
        "operator_effort": json.loads(_canonical_bytes(operator_effort)),
        "safety": safety,
    }


def _record_issues(record: dict[str, Any], expected_route: str) -> list[str]:
    issues: list[str] = []
    if record.get("schema") != ROUTE_RUN_SCHEMA:
        issues.append("ROUTE_RUN_SCHEMA_INVALID")
    if int(record.get("schema_version") or 0) != ROUTE_RECORD_VERSION:
        issues.append("ROUTE_RUN_SCHEMA_VERSION_UNSUPPORTED")
    if str(record.get("route") or "") != expected_route:
        issues.append("ROUTE_IDENTITY_MISMATCH")
    observation = record.get("observation")
    try:
        computed_fingerprint = observation_fingerprint(observation)
    except (TypeError, ValueError):
        issues.append("OBSERVATION_PROVENANCE_INVALID")
    else:
        if (
            str(record.get("observation_fingerprint") or "")
            != computed_fingerprint
        ):
            issues.append("OBSERVATION_FINGERPRINT_INVALID")
    safety = record.get("safety")
    if not isinstance(safety, dict) or any(
        bool(safety.get(field))
        for field in (
            "physical_action_submitted",
            "controller_called",
            "control_mode_changed",
        )
    ):
        issues.append("ROUTE_RUN_NOT_NONPHYSICAL")
    lifecycle = record.get("lifecycle")
    if not isinstance(lifecycle, dict):
        issues.append("LIFECYCLE_EVIDENCE_MISSING")
    else:
        if lifecycle.get("owned_session_count_after") != 0:
            issues.append("OWNED_ESTIMATOR_SESSION_LEAK")
        if lifecycle.get("gpu_resources_released") is not True:
            issues.append("GPU_RELEASE_UNCONFIRMED")
        if (
            expected_route == SKILL_LOCAL_ROUTE
            and lifecycle.get("backend_closed") is not True
        ):
            issues.append("SKILL_LOCAL_BACKEND_NOT_CLOSED")
    outcome = record.get("outcome")
    if not isinstance(outcome, dict):
        issues.append("OUTCOME_MISSING")
    elif str(outcome.get("status") or "") == "SUCCEEDED":
        try:
            transform_from_payload(outcome.get("world_from_base"))
        except (AttributeError, KeyError, TypeError, ValueError):
            issues.append("SUCCESS_TRANSFORM_INVALID")
    elif str(outcome.get("status") or "") in {"FAILED", "REJECTED"}:
        error = outcome.get("error")
        if (
            not isinstance(error, dict)
            or not str(error.get("code") or "")
            or not str(error.get("message") or "")
        ):
            issues.append("FAILURE_CLARITY_INCOMPLETE")
    return issues


def compare_route_run_records(
    provider_record: dict[str, Any],
    local_record: dict[str, Any],
    *,
    thresholds: dict[str, float] | None = None,
) -> dict[str, Any]:
    limits = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    provider_issues = _record_issues(
        provider_record,
        PROVIDER_COMPATIBILITY_ROUTE,
    )
    local_issues = _record_issues(local_record, SKILL_LOCAL_ROUTE)
    computed_fingerprints: list[str] = []
    for record in (provider_record, local_record):
        try:
            computed_fingerprints.append(
                observation_fingerprint(record.get("observation"))
            )
        except (TypeError, ValueError):
            computed_fingerprints.append("")
    fingerprints = set(computed_fingerprints)
    same_case = (
        str(provider_record.get("case_id") or "")
        == str(local_record.get("case_id") or "")
    )
    input_match = len(fingerprints) == 1 and "" not in fingerprints and same_case
    if not input_match:
        provider_issues.append("IDENTICAL_OBSERVATION_REQUIRED")
        local_issues.append("IDENTICAL_OBSERVATION_REQUIRED")

    provider_outcome = provider_record.get("outcome") or {}
    local_outcome = local_record.get("outcome") or {}
    both_succeeded = (
        provider_outcome.get("status") == "SUCCEEDED"
        and local_outcome.get("status") == "SUCCEEDED"
        and "SUCCESS_TRANSFORM_INVALID" not in provider_issues
        and "SUCCESS_TRANSFORM_INVALID" not in local_issues
    )
    translation_delta_m: float | None = None
    rotation_delta_rad: float | None = None
    if both_succeeded:
        provider_transform = transform_from_payload(
            provider_outcome["world_from_base"]
        )
        local_transform = transform_from_payload(
            local_outcome["world_from_base"]
        )
        translation_delta_m = float(
            np.linalg.norm(
                provider_transform[:3, 3] - local_transform[:3, 3]
            )
        )
        rotation_delta_rad = _rotation_delta_rad(
            provider_transform,
            local_transform,
        )

    repeatability_pass = True
    for record in (provider_record, local_record):
        repeatability = record.get("repeatability") or {}
        translation_span = repeatability.get("maximum_translation_span_m")
        rotation_span = repeatability.get("maximum_rotation_span_rad")
        if (
            translation_span is None
            or rotation_span is None
            or float(translation_span)
            > float(limits["maximum_repeatability_translation_span_m"])
            or float(rotation_span)
            > float(limits["maximum_repeatability_rotation_span_rad"])
        ):
            repeatability_pass = False

    accuracy_pass = bool(
        both_succeeded
        and translation_delta_m is not None
        and rotation_delta_rad is not None
        and translation_delta_m
        <= float(limits["maximum_translation_delta_m"])
        and rotation_delta_rad
        <= float(limits["maximum_rotation_delta_rad"])
    )
    eligible = not provider_issues and not local_issues
    passed = eligible and accuracy_pass and repeatability_pass
    return {
        "schema": ROUTE_COMPARISON_SCHEMA,
        "schema_version": ROUTE_RECORD_VERSION,
        "created_at_us": time.time_ns() // 1000,
        "case_id": str(provider_record.get("case_id") or ""),
        "status": "PASS" if passed else "FAIL",
        "identical_observation": input_match,
        "observation_fingerprint": (
            next(iter(fingerprints)) if input_match else None
        ),
        "thresholds": limits,
        "comparison": {
            "translation_delta_m": translation_delta_m,
            "rotation_delta_rad": rotation_delta_rad,
            "accuracy_pass": accuracy_pass,
            "repeatability_pass": repeatability_pass,
            "latency_ms": {
                PROVIDER_COMPATIBILITY_ROUTE: provider_outcome.get(
                    "latency_ms"
                ),
                SKILL_LOCAL_ROUTE: local_outcome.get("latency_ms"),
            },
            "operator_effort": {
                PROVIDER_COMPATIBILITY_ROUTE: provider_record.get(
                    "operator_effort"
                ),
                SKILL_LOCAL_ROUTE: local_record.get("operator_effort"),
            },
        },
        "route_issues": {
            PROVIDER_COMPATIBILITY_ROUTE: list(dict.fromkeys(provider_issues)),
            SKILL_LOCAL_ROUTE: list(dict.fromkeys(local_issues)),
        },
        "physical_action_submitted": False,
        "motion_usable": False,
        "review_state": "COMPARISON_EVIDENCE_ONLY",
    }
