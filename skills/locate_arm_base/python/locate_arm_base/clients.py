from __future__ import annotations

from pathlib import Path
import time
from typing import Any
from urllib.parse import quote

import httpx
import numpy as np
from PIL import Image
from midbrain_bufferref import copy_buffer_refs

from .math3d import transform_from_translation_quaternion


def _manager_error_detail(response: httpx.Response) -> str:
    error_code = ""
    message = ""
    try:
        payload = response.json()
    except (ValueError, TypeError):
        payload = None
    if isinstance(payload, dict):
        error_code = str(payload.get("error_code") or "").strip()
        message = str(payload.get("error") or payload.get("detail") or "").strip()
    if not message:
        message = response.text.strip()
    message = " ".join(message.split())[:2000]
    if not message:
        message = response.reason_phrase or "Manager returned no diagnostic body"
    if error_code and not message.startswith(f"{error_code}:"):
        return f"{error_code}: {message}"
    return message


def _raise_manager_provider_error(
    response: httpx.Response,
    *,
    provider_id: str,
    domain_code: str,
) -> None:
    if response.is_success:
        return
    detail = _manager_error_detail(response)
    raise RuntimeError(
        f"{domain_code}: Manager HTTP {response.status_code} could not make "
        f"Provider {provider_id!r} HOT; {detail}"
    )


class MidbrainClients:
    def __init__(
        self,
        manager_url: str,
        fabric_url: str,
        provider_id: str,
        sam2_provider_id: str = "perception.sam2_scene_tracker",
        timeout_s: float = 30.0,
    ) -> None:
        self.manager_url = manager_url.rstrip("/")
        self.fabric_url = fabric_url.rstrip("/")
        self.provider_id = provider_id
        self.sam2_provider_id = sam2_provider_id
        self.http = httpx.Client(timeout=float(timeout_s))

    def latest(self, stream: str) -> dict[str, Any]:
        response = self.http.get(
            f"{self.fabric_url}/v1/latest/{quote(stream, safe='')}"
        )
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise RuntimeError(f"Fabric stream {stream} returned a non-object")
        return value

    def transform(
        self,
        from_frame: str,
        to_frame: str,
        at_us: int,
        max_extrapolation_us: int,
        session_epoch: str | None = None,
    ) -> tuple[np.ndarray, dict[str, Any]]:
        params: dict[str, Any] = {
            "from_frame": from_frame,
            "to_frame": to_frame,
            "at_us": int(at_us),
            "max_extrapolation_us": int(max_extrapolation_us),
        }
        if session_epoch:
            params["session_epoch"] = str(session_epoch)
        response = self.http.get(
            f"{self.fabric_url}/v1/transform",
            params=params,
        )
        if response.status_code == 404:
            raise RuntimeError(
                "WORLD_AXIS_REQUIRED: Fabric has no timestamped transform from "
                f"{from_frame} to {to_frame}. Establish the current world axis, "
                "then retry locate_arm_base."
            )
        response.raise_for_status()
        value = response.json()
        return transform_from_translation_quaternion(
            value["translation_m"], value["rotation_xyzw"]
        ), value

    def current_world_axis(
        self,
        stream: str = "localization.vio.status",
        *,
        required_convention: str = "MIDBRAIN_X_FORWARD_Y_LEFT_Z_UP_V2",
    ) -> dict[str, Any]:
        observation = self.latest(stream)
        data = observation.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("WORLD_AXIS_REQUIRED: VIO status has no data object")
        tracking_state = str(data.get("tracking_state") or "")
        world_frame = str(data.get("world_frame") or "").strip()
        session_epoch = str(data.get("session_epoch") or "").strip()
        convention_id = str(data.get("convention_id") or "").strip()
        if observation.get("valid") is False or tracking_state != "TRACKING":
            raise RuntimeError(
                "WORLD_AXIS_REQUIRED: Local VIO is not publishing a valid TRACKING epoch"
            )
        if not world_frame or not session_epoch:
            raise RuntimeError(
                "WORLD_AXIS_REQUIRED: Local VIO TRACKING status lacks epoch identity"
            )
        if required_convention and convention_id != required_convention:
            raise RuntimeError(
                "WORLD_AXIS_REQUIRED: Local VIO convention does not match "
                f"{required_convention}"
            )
        return {
            "world_frame": world_frame,
            "session_epoch": session_epoch,
            "tracking_state": tracking_state,
            "convention_id": convention_id,
            "status_observed_at_us": observation.get("observed_at_us"),
            "provider_id": observation.get("provider_id"),
            "provider_instance_id": observation.get("provider_instance_id"),
            "boot_id": observation.get("boot_id"),
        }

    def active_arm_profile_state(
        self, stream: str = "robot_arm.assembly_state"
    ) -> dict[str, Any]:
        observation = self.latest(stream)
        state = observation.get("data")
        if not isinstance(state, dict):
            raise RuntimeError("active robot assembly observation has no state object")
        if state.get("schema") != "midbrain.robot_assembly_state":
            raise RuntimeError("active robot assembly state schema is unsupported")
        if observation.get("valid") is False:
            raise RuntimeError("active robot assembly observation is invalid")
        return state

    def ensure_active_arm_profile_state(
        self,
        provider_id: str,
        stream: str = "robot_arm.assembly_state",
        *,
        timeout_s: float = 15.0,
        poll_interval_s: float = 0.1,
    ) -> dict[str, Any]:
        """Request Manager-owned residency and wait for the Provider-owned state."""
        normalized_provider_id = str(provider_id).strip()
        if not normalized_provider_id:
            raise ValueError("arm Provider ID must be non-empty")
        try:
            hot = self.http.post(
                f"{self.manager_url}/v1/providers/"
                f"{quote(normalized_provider_id, safe='')}/hot"
            )
        except httpx.RequestError as error:
            raise RuntimeError(
                "ARM_PROVIDER_READINESS_FAILED: Manager could not make selected "
                f"arm Provider {normalized_provider_id!r} HOT: {error}"
            ) from error
        _raise_manager_provider_error(
            hot,
            provider_id=normalized_provider_id,
            domain_code="ARM_PROVIDER_READINESS_FAILED",
        )

        timeout_s = max(0.0, float(timeout_s))
        poll_interval_s = max(0.0, float(poll_interval_s))
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                return self.active_arm_profile_state(stream)
            except httpx.HTTPStatusError as error:
                if error.response.status_code != 404:
                    raise RuntimeError(
                        "ARM_ASSEMBLY_STATE_UNAVAILABLE: Fabric rejected selected "
                        f"arm assembly stream {stream!r}: {error}"
                    ) from error
            except httpx.HTTPError as error:
                raise RuntimeError(
                    "ARM_ASSEMBLY_STATE_UNAVAILABLE: Fabric could not be queried for "
                    f"selected arm assembly stream {stream!r}: {error}"
                ) from error
            remaining_s = deadline - time.monotonic()
            if remaining_s <= 0.0:
                break
            time.sleep(min(poll_interval_s, remaining_s))
        raise RuntimeError(
            "ARM_ASSEMBLY_STATE_REQUIRED: selected arm Provider "
            f"{normalized_provider_id!r} did not publish {stream!r} within "
            f"{timeout_s:.1f}s after Manager accepted HOT residency"
        )

    def estimate_pose(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.http.post(
            f"{self.manager_url}/v1/providers/{quote(self.provider_id, safe='')}/request",
            json={
                "action": "estimate",
                "payload": payload,
                "request_id": payload.get("request_id"),
                "related_skill_id": "locate_arm_base",
            },
        )
        response.raise_for_status()
        value = response.json()
        measurement = value.get("measurement") if isinstance(value, dict) else None
        if not isinstance(measurement, dict):
            raise RuntimeError("FoundationPose Provider returned no measurement")
        return measurement

    def segment_mask(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.http.post(
            f"{self.manager_url}/v1/providers/{quote(self.sam2_provider_id, safe='')}/request",
            json={
                "action": "segment_image",
                "payload": payload,
                "request_id": payload.get("request_id"),
                "related_skill_id": "locate_arm_base",
            },
        )
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict) or value.get("status") != "SEGMENTED":
            raise RuntimeError("SAM2 Provider returned no one-shot segmentation")
        return value

    def ensure_foundation_pose_hot(self) -> dict[str, Any]:
        return self._ensure_provider_hot(self.provider_id)

    def ensure_sam2_hot(self) -> dict[str, Any]:
        return self._ensure_provider_hot(self.sam2_provider_id)

    def _ensure_provider_hot(self, provider_id: str) -> dict[str, Any]:
        try:
            response = self.http.post(
                f"{self.manager_url}/v1/providers/{quote(provider_id, safe='')}/hot"
            )
        except httpx.RequestError as error:
            raise RuntimeError(
                "PROVIDER_READINESS_FAILED: Manager could not be reached while making "
                f"Provider {provider_id!r} HOT: {error}"
            ) from error
        _raise_manager_provider_error(
            response,
            provider_id=provider_id,
            domain_code="PROVIDER_READINESS_FAILED",
        )
        value = response.json()
        return value if isinstance(value, dict) else {}

    def publish_candidate(self, candidate: dict[str, Any]) -> None:
        observation = {
            "schema": "midbrain.skill.locate_arm_base.candidate_observation",
            "schema_version": 1,
            "stream": "calibration.arm_base.candidate",
            "provider_id": "skill.locate_arm_base",
            "provider_instance_id": str(candidate["run_id"]),
            "boot_id": str(candidate["run_id"]),
            "sequence": 1,
            "observed_at_us": int(candidate["observed_at_us"]),
            "freshness_ms": 86400000,
            "frame_id": str(candidate["parent_frame"]),
            "coordinate_frame": "RIGHT_HANDED_Z_UP",
            "calibration_revision": "CANDIDATE_NOT_ACTIVE",
            "clock_domain": "system_wall_clock",
            "related_skill_id": "locate_arm_base",
            "valid": True,
            "data": candidate,
        }
        response = self.http.post(f"{self.fabric_url}/v1/observations", json=observation)
        response.raise_for_status()

    def snapshot_latest_rgbd(self, output_dir: Path) -> dict[str, Any]:
        capture_attempt_count = 0
        while True:
            capture_attempt_count += 1
            bundle = self.latest("camera.rgbd.bundle")
            calibration = self.latest("camera.calibration")
            data = bundle.get("data") if isinstance(bundle.get("data"), dict) else {}
            rgb_ref = data.get("rgb")
            depth_ref = data.get("depth_aligned_to_rgb")
            if not isinstance(rgb_ref, dict) or not isinstance(depth_ref, dict):
                raise RuntimeError("current RGB-D bundle lacks aligned BufferRefs")
            try:
                rgb, depth_m = _copy_rgbd(rgb_ref, depth_ref)
                break
            except RuntimeError as error:
                message = str(error).lower()
                stale_ref = "bufferref" in message and (
                    "expired" in message or "recycled" in message
                )
                if not stale_ref or capture_attempt_count >= 3:
                    raise
                time.sleep(0.01)
        device_info = self.latest("camera.device_info")
        for label, observation in (
            ("calibration", calibration),
            ("device information", device_info),
        ):
            for field in ("provider_id", "provider_instance_id", "boot_id"):
                expected = str(bundle.get(field) or "").strip()
                observed = str(observation.get(field) or "").strip()
                if expected and observed and expected != observed:
                    raise RuntimeError(
                        f"camera {label} {field} does not match the captured RGB-D bundle"
                    )
        output_dir.mkdir(parents=True, exist_ok=True)
        rgb_path, depth_path = output_dir / "rgb.png", output_dir / "depth_m.npy"
        Image.fromarray(rgb).save(rgb_path)
        np.save(depth_path, depth_m, allow_pickle=False)
        calibration_data = calibration.get("data")
        calibration_data = calibration_data if isinstance(calibration_data, dict) else {}
        intrinsics = calibration_data.get("rgb_intrinsic")
        if not isinstance(intrinsics, dict):
            raise RuntimeError("camera calibration lacks RGB intrinsics")
        frames = data.get("coordinate_frames")
        frames = frames if isinstance(frames, dict) else {}
        observed_at_us = int(
            rgb_ref.get("global_timestamp_us")
            or rgb_ref.get("system_timestamp_us")
            or bundle.get("observed_at_us")
            or time.time_ns() // 1000
        )
        return {
            "rgb_path": str(rgb_path),
            "depth_npy_path": str(depth_path),
            "camera_intrinsics": intrinsics,
            "camera_frame": str(frames.get("rgb") or "femto_bolt_color_optical_frame"),
            "observed_at_us": observed_at_us,
            "source_observations": {
                "bundle": bundle,
                "calibration": calibration,
                "device_info": device_info,
                "capture_attempt_count": capture_attempt_count,
            },
        }

    def close(self) -> None:
        self.http.close()


def _copy_rgbd(
    rgb_ref: dict[str, Any], depth_ref: dict[str, Any]
) -> tuple[np.ndarray, np.ndarray]:
    mapping_name = str(rgb_ref.get("mapping_name") or "")
    if not mapping_name or mapping_name != str(depth_ref.get("mapping_name") or ""):
        raise RuntimeError("aligned RGB and depth BufferRefs must share one mapping")
    rgb_payload, depth_payload = copy_buffer_refs([rgb_ref, depth_ref])
    rgb_format = str(rgb_ref.get("format_name") or "").upper()
    width, height = int(rgb_ref["width"]), int(rgb_ref["height"])
    if rgb_format in {"JPEG", "JPG", "MJPEG", "MJPG"}:
        from io import BytesIO

        rgb = np.asarray(Image.open(BytesIO(rgb_payload)).convert("RGB"), dtype=np.uint8)
    else:
        channels = 4 if rgb_format in {"RGBA", "BGRA"} else 3
        raw = np.frombuffer(rgb_payload, np.uint8).reshape(height, width, channels)
        if rgb_format == "RGB":
            rgb = raw.copy()
        elif rgb_format == "BGR":
            rgb = raw[:, :, ::-1].copy()
        elif rgb_format == "RGBA":
            rgb = raw[:, :, :3].copy()
        elif rgb_format == "BGRA":
            rgb = raw[:, :, [2, 1, 0]].copy()
        else:
            raise RuntimeError(f"unsupported current RGB format {rgb_format!r}")
    depth_width, depth_height = int(depth_ref["width"]), int(depth_ref["height"])
    depth = np.frombuffer(depth_payload, "<u2", count=depth_width * depth_height)
    scale_mm = float(depth_ref.get("depth_value_scale_mm") or 1.0)
    depth_m = depth.reshape(depth_height, depth_width).astype(np.float32) * scale_mm / 1000.0
    if depth_m.shape != rgb.shape[:2]:
        raise RuntimeError("aligned RGB and depth snapshot shapes differ")
    return rgb, depth_m
