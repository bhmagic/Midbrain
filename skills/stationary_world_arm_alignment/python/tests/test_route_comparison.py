from __future__ import annotations

import copy
import hashlib
import json

import pytest
from stationary_world_arm_alignment.foundation_engine import (
    PROVIDER_COMPATIBILITY_ROUTE,
    SKILL_LOCAL_ROUTE,
)
from stationary_world_arm_alignment.math3d import transform_payload
from stationary_world_arm_alignment.route_comparison import (
    build_route_run_record,
    compare_route_run_records,
    load_replay_observation,
)


def _transform(x: float = 0.0) -> dict:
    import numpy as np

    value = np.eye(4, dtype=np.float64)
    value[0, 3] = x
    return transform_payload(value)


def _observation() -> dict:
    return {
        "capture_id": "replay-capture-1",
        "rgb_sha256": "a" * 64,
        "registered_depth_sha256": "b" * 64,
        "camera_route_id": "camera.rgbd.shared_memory.flexible.v1",
        "camera_calibration_revision": "calibration-1",
        "camera_boot_id": "camera-boot-1",
        "vio_session_epoch": "vio-epoch-1",
        "frame_timestamp_us": 123456,
        "gripper_configuration": "INSTALLED",
    }


def _record(route: str, *, x: float = 0.0) -> dict:
    lifecycle = {
        "owned_session_count_after": 0,
        "gpu_resources_released": True,
        "backend_closed": route == SKILL_LOCAL_ROUTE,
    }
    return build_route_run_record(
        case_id="nominal-installed-gripper",
        route=route,
        observation=_observation(),
        status="SUCCEEDED",
        latency_ms=25.0 if route == SKILL_LOCAL_ROUTE else 50.0,
        world_from_base=_transform(x),
        sample_transforms=[
            _transform(x),
            _transform(x + 0.001),
        ],
        lifecycle=lifecycle,
        operator_effort={
            "authorization_count": 0,
            "manual_adjustment_count": 0,
            "development_override_count": 0,
        },
    )


def test_identical_observation_comparison_passes_and_is_not_motion_usable() -> None:
    result = compare_route_run_records(
        _record(PROVIDER_COMPATIBILITY_ROUTE),
        _record(SKILL_LOCAL_ROUTE, x=0.002),
    )

    assert result["status"] == "PASS"
    assert result["identical_observation"] is True
    assert result["physical_action_submitted"] is False
    assert result["motion_usable"] is False
    assert result["comparison"]["translation_delta_m"] == 0.002


def test_comparison_rejects_different_capture_identity() -> None:
    provider = _record(PROVIDER_COMPATIBILITY_ROUTE)
    local = _record(SKILL_LOCAL_ROUTE)
    local = copy.deepcopy(local)
    local["observation"]["camera_boot_id"] = "camera-boot-2"
    local["observation_fingerprint"] = "different"

    result = compare_route_run_records(provider, local)

    assert result["status"] == "FAIL"
    assert result["identical_observation"] is False
    assert "IDENTICAL_OBSERVATION_REQUIRED" in result["route_issues"][
        SKILL_LOCAL_ROUTE
    ]


def test_comparison_rejects_unreleased_skill_local_backend() -> None:
    provider = _record(PROVIDER_COMPATIBILITY_ROUTE)
    local = _record(SKILL_LOCAL_ROUTE)
    local["lifecycle"]["backend_closed"] = False
    local["lifecycle"]["gpu_resources_released"] = False

    result = compare_route_run_records(provider, local)

    assert result["status"] == "FAIL"
    assert result["route_issues"][SKILL_LOCAL_ROUTE] == [
        "GPU_RELEASE_UNCONFIRMED",
        "SKILL_LOCAL_BACKEND_NOT_CLOSED",
    ]


def test_failed_route_requires_structured_failure_clarity() -> None:
    provider = _record(PROVIDER_COMPATIBILITY_ROUTE)
    local = build_route_run_record(
        case_id="nominal-installed-gripper",
        route=SKILL_LOCAL_ROUTE,
        observation=_observation(),
        status="FAILED",
        latency_ms=10.0,
        world_from_base=None,
        sample_transforms=[],
        lifecycle={
            "owned_session_count_after": 0,
            "gpu_resources_released": True,
            "backend_closed": True,
        },
        operator_effort={
            "authorization_count": 0,
            "manual_adjustment_count": 0,
            "development_override_count": 0,
        },
        error={"code": "POSE_REJECTED", "message": "overlay validation failed"},
    )

    result = compare_route_run_records(provider, local)

    assert result["status"] == "FAIL"
    assert "FAILURE_CLARITY_INCOMPLETE" not in result["route_issues"][
        SKILL_LOCAL_ROUTE
    ]


def test_missing_lifecycle_session_evidence_is_rejected() -> None:
    provider = _record(PROVIDER_COMPATIBILITY_ROUTE)
    local = _record(SKILL_LOCAL_ROUTE)
    del local["lifecycle"]["owned_session_count_after"]

    result = compare_route_run_records(provider, local)

    assert result["status"] == "FAIL"
    assert "OWNED_ESTIMATOR_SESSION_LEAK" in result["route_issues"][
        SKILL_LOCAL_ROUTE
    ]


def test_replay_bundle_builds_one_verified_observation_identity(tmp_path) -> None:
    payload_root = tmp_path / "payloads"
    payload_root.mkdir()
    rgb = b"captured-rgb"
    depth = b"captured-registered-depth"
    (payload_root / "rgb.bin").write_bytes(rgb)
    (payload_root / "depth.bin").write_bytes(depth)
    manifest = {
        "schema": "physical_agent.phase5_replay_bundle",
        "schema_version": 1,
        "bundle_id": "capture-1",
        "payloads": {
            "rgb": {
                "path": "payloads/rgb.bin",
                "sha256": hashlib.sha256(rgb).hexdigest(),
            },
            "registered_depth": {
                "path": "payloads/depth.bin",
                "sha256": hashlib.sha256(depth).hexdigest(),
            },
        },
        "records": {
            "fabric": {
                "route_observation": {
                    "data": {
                        "preferred_route_id": (
                            "camera.rgbd.shared_memory.flexible.v1"
                        )
                    }
                },
                "rgbd_bundle_observation": {
                    "provider_id": "camera.femto_bolt",
                    "provider_instance_id": "camera-instance",
                    "boot_id": "camera-boot",
                    "calibration_revision": "calibration-1",
                    "observed_at_us": 123456,
                },
                "optional_streams": {
                    "localization.body.pose": {
                        "data": {"session_epoch": "vio-epoch"}
                    }
                },
            }
        },
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    first = load_replay_observation(
        manifest_path,
        gripper_configuration="installed",
    )
    second = load_replay_observation(
        manifest_path,
        gripper_configuration="installed",
    )

    assert first == second
    assert first["payloads_verified"] is True
    assert first["camera_provider_instance_id"] == "camera-instance"
    assert first["gripper_configuration"] == "INSTALLED"


def test_replay_observation_rejects_payload_mutation_or_path_escape(
    tmp_path,
) -> None:
    payload_root = tmp_path / "payloads"
    payload_root.mkdir()
    (payload_root / "rgb.bin").write_bytes(b"changed")
    (payload_root / "depth.bin").write_bytes(b"depth")
    manifest = {
        "schema": "physical_agent.phase5_replay_bundle",
        "schema_version": 1,
        "bundle_id": "capture-1",
        "payloads": {
            "rgb": {
                "path": "payloads/rgb.bin",
                "sha256": hashlib.sha256(b"original").hexdigest(),
            },
            "registered_depth": {
                "path": "payloads/depth.bin",
                "sha256": hashlib.sha256(b"depth").hexdigest(),
            },
        },
        "records": {
            "fabric": {
                "route_observation": {
                    "data": {"preferred_route_id": "route"}
                },
                "rgbd_bundle_observation": {
                    "provider_id": "camera",
                    "provider_instance_id": "instance",
                    "boot_id": "boot",
                    "calibration_revision": "calibration",
                    "observed_at_us": 123,
                },
                "optional_streams": {
                    "localization.body.pose": {
                        "data": {"session_epoch": "epoch"}
                    }
                },
            }
        },
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="does not match"):
        load_replay_observation(
            manifest_path,
            gripper_configuration="INSTALLED",
        )

    manifest["payloads"]["rgb"]["path"] = "../outside.bin"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="escapes"):
        load_replay_observation(
            manifest_path,
            gripper_configuration="INSTALLED",
        )
