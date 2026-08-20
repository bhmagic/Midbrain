from __future__ import annotations

import httpx
import numpy as np
import pytest

from locate_arm_base.clients import MidbrainClients


def test_arm_provider_readiness_requests_manager_and_waits_for_assembly() -> None:
    requests: list[tuple[str, str]] = []
    assembly_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal assembly_attempts
        requests.append((request.method, request.url.path))
        if request.url.path == "/v1/providers/robot_arm.rebot_dm/hot":
            return httpx.Response(200, json={"residency": "HOT"})
        if request.url.path == "/v1/latest/robot_arm.assembly_state":
            assembly_attempts += 1
            if assembly_attempts == 1:
                return httpx.Response(404, json={"error": "stream unavailable"})
            return httpx.Response(
                200,
                json={
                    "valid": True,
                    "data": {
                        "schema": "midbrain.robot_assembly_state",
                        "arm_provider_id": "robot_arm.rebot_dm",
                    },
                },
            )
        return httpx.Response(500)

    clients = MidbrainClients("http://manager", "http://fabric", "pose")
    clients.http.close()
    clients.http = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        state = clients.ensure_active_arm_profile_state(
            "robot_arm.rebot_dm",
            timeout_s=0.1,
            poll_interval_s=0.0,
        )
    finally:
        clients.close()
    assert state["arm_provider_id"] == "robot_arm.rebot_dm"
    assert requests == [
        ("POST", "/v1/providers/robot_arm.rebot_dm/hot"),
        ("GET", "/v1/latest/robot_arm.assembly_state"),
        ("GET", "/v1/latest/robot_arm.assembly_state"),
    ]


def test_arm_provider_readiness_reports_domain_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"residency": "HOT"})
        return httpx.Response(404, json={"error": "stream unavailable"})

    clients = MidbrainClients("http://manager", "http://fabric", "pose")
    clients.http.close()
    clients.http = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(RuntimeError, match="ARM_ASSEMBLY_STATE_REQUIRED"):
            clients.ensure_active_arm_profile_state(
                "robot_arm.rebot_dm",
                timeout_s=0.0,
                poll_interval_s=0.0,
            )
    finally:
        clients.close()


def test_latest_rgbd_retries_an_expired_bufferref_snapshot(
    tmp_path, monkeypatch
) -> None:
    copy_attempts = 0
    rgb_ref = {
        "mapping_name": "camera-test",
        "format_name": "RGB",
        "width": 2,
        "height": 1,
        "global_timestamp_us": 123456,
    }
    depth_ref = {
        "mapping_name": "camera-test",
        "format_name": "Z16",
        "width": 2,
        "height": 1,
        "depth_value_scale_mm": 1.0,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/camera.rgbd.bundle"):
            return httpx.Response(
                200,
                json={
                    "observed_at_us": 123456,
                    "data": {
                        "rgb": rgb_ref,
                        "depth_aligned_to_rgb": depth_ref,
                        "coordinate_frames": {"rgb": "test_camera"},
                    },
                },
            )
        if request.url.path.endswith("/camera.calibration"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "rgb_intrinsic": {
                            "fx": 100.0,
                            "fy": 100.0,
                            "cx": 1.0,
                            "cy": 0.5,
                        }
                    }
                },
            )
        if request.url.path.endswith("/camera.device_info"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "canonical_device_id": "orbbec:femto-bolt:test-camera"
                    }
                },
            )
        return httpx.Response(404)

    def copy_refs(refs):
        nonlocal copy_attempts
        copy_attempts += 1
        if copy_attempts == 1:
            raise RuntimeError("BufferRef has expired or slot recycled")
        return bytes([10, 20, 30, 40, 50, 60]), np.array(
            [1000, 2000], dtype="<u2"
        ).tobytes()

    monkeypatch.setattr("locate_arm_base.clients.copy_buffer_refs", copy_refs)
    clients = MidbrainClients("http://manager", "http://fabric", "pose")
    clients.http.close()
    clients.http = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        capture = clients.snapshot_latest_rgbd(tmp_path)
    finally:
        clients.close()
    assert copy_attempts == 2
    assert capture["source_observations"]["capture_attempt_count"] == 2
    assert capture["camera_frame"] == "test_camera"
    assert capture["source_observations"]["device_info"]["data"][
        "canonical_device_id"
    ] == "orbbec:femto-bolt:test-camera"
    assert np.allclose(np.load(capture["depth_npy_path"]), [[1.0, 2.0]])
