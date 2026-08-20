from __future__ import annotations

import asyncio
from dataclasses import dataclass
from io import BytesIO
import time
from typing import Any

import numpy as np
from PIL import Image
from midbrain_bufferref import copy_buffer_refs

from .fabric_client import FabricClient


WORLD_CONVENTION_ID = "MIDBRAIN_X_FORWARD_Y_LEFT_Z_UP_V2"
CAMERA_OPTICAL_CONVENTION_ID = "CAMERA_OPTICAL_X_RIGHT_Y_DOWN_Z_FORWARD_V1"


@dataclass(frozen=True)
class RgbdFrame:
    rgb: np.ndarray
    depth_m: np.ndarray
    intrinsics: dict[str, Any]
    timestamp_us: int
    frame_number: int
    camera_frame: str
    session_epoch: str | None
    world_frame: str | None
    calibration_revision: str | None
    observations: dict[str, Any]


class RgbdFrameCapture:
    """Copy one synchronized camera bundle without owning camera policy."""

    def __init__(self, fabric: FabricClient, camera_frame: str) -> None:
        self.fabric = fabric
        self.camera_frame = camera_frame

    async def capture(
        self,
        attempts: int = 8,
        retry_delay_s: float = 0.04,
        *,
        require_vio: bool = True,
    ) -> RgbdFrame:
        calibration_observation = await self.fabric.latest_optional("camera.calibration")
        route_observation = await self.fabric.latest_optional("camera.rgbd.data_routes")
        device_observation = await self.fabric.latest_optional("camera.device_info")
        if not calibration_observation:
            raise RuntimeError("camera calibration is unavailable")
        calibration = calibration_observation.get("data") or {}
        conventions = calibration.get("coordinate_conventions") or {}
        if conventions.get("color") != CAMERA_OPTICAL_CONVENTION_ID:
            raise RuntimeError("camera calibration lacks the required optical convention")
        intrinsics = calibration.get("rgb_intrinsic") or {}
        if float(intrinsics.get("fx") or 0) <= 0:
            raise RuntimeError("camera RGB intrinsics are invalid")

        last_error: Exception | None = None
        for attempt in range(attempts):
            bundle_observation = await self.fabric.latest_optional("camera.rgbd.bundle")
            if not bundle_observation:
                raise RuntimeError("RGB-D bundle is unavailable")
            bundle = bundle_observation.get("data") or {}
            bundle_conventions = bundle.get("coordinate_conventions") or {}
            if (
                bundle_conventions.get("rgb") != CAMERA_OPTICAL_CONVENTION_ID
                or bundle_conventions.get("aligned_depth")
                != CAMERA_OPTICAL_CONVENTION_ID
            ):
                raise RuntimeError("RGB-D bundle lacks the required optical convention")
            rgb_ref = bundle.get("rgb")
            depth_ref = bundle.get("depth_aligned_to_rgb")
            if not isinstance(rgb_ref, dict) or not isinstance(depth_ref, dict):
                raise RuntimeError("RGB-D bundle lacks aligned BufferRefs")
            mapping_name = str(rgb_ref.get("mapping_name") or "")
            if not mapping_name or mapping_name != str(depth_ref.get("mapping_name") or ""):
                raise RuntimeError("RGB-D BufferRefs do not share one mapping")
            try:
                rgb_payload, depth_payload = copy_buffer_refs([rgb_ref, depth_ref])
            except Exception as error:
                last_error = error
                if attempt + 1 < attempts:
                    await asyncio.sleep(retry_delay_s)
                    continue
                break
            copied_at_us = time.time_ns() // 1000
            rgb = self._decode_rgb(rgb_payload, rgb_ref)
            depth_m = self._decode_depth(depth_payload, depth_ref)
            if rgb.shape[:2] != depth_m.shape:
                raise RuntimeError("aligned depth grid does not match the RGB grid")
            pose_observation: dict[str, Any] | None = None
            vio_observation: dict[str, Any] | None = None
            session_epoch: str | None = None
            world_frame: str | None = None
            if require_vio:
                pose_observation, vio_observation = await asyncio.gather(
                    self.fabric.latest_optional("localization.body.pose"),
                    self.fabric.latest_optional("localization.vio.status"),
                )
                if not pose_observation or not vio_observation:
                    raise RuntimeError("VIO pose or tracking status is unavailable")
                pose = pose_observation.get("data") or {}
                vio = vio_observation.get("data") or {}
                if (
                    pose.get("convention_id") != WORLD_CONVENTION_ID
                    or vio.get("convention_id") != WORLD_CONVENTION_ID
                ):
                    raise RuntimeError("VIO does not declare the convention-V2 Z-up world")
                session_epoch = str(
                    pose.get("session_epoch") or vio.get("session_epoch") or ""
                )
                world_frame = str(pose.get("world_frame") or "")
                if not session_epoch or not world_frame:
                    raise RuntimeError("VIO world-frame identity is incomplete")
            timestamp_us = self._reference_timestamp(rgb_ref)
            if timestamp_us <= 0:
                timestamp_us = int(bundle_observation.get("observed_at_us") or 0)
            return RgbdFrame(
                rgb=rgb,
                depth_m=depth_m,
                intrinsics=dict(intrinsics),
                timestamp_us=timestamp_us,
                frame_number=int(rgb_ref.get("frame_number") or -1),
                camera_frame=self.camera_frame,
                session_epoch=session_epoch,
                world_frame=world_frame,
                calibration_revision=(
                    str(
                        calibration.get("calibration_revision")
                        or calibration_observation.get("calibration_revision")
                    )
                    if (
                        calibration.get("calibration_revision") is not None
                        or calibration_observation.get("calibration_revision") is not None
                    )
                    else None
                ),
                observations={
                    "bundle": bundle_observation,
                    "route": route_observation,
                    "device_info": device_observation,
                    "calibration": calibration_observation,
                    "body_pose": pose_observation,
                    "vio_status": vio_observation,
                    "capture": {
                        "copy_attempt": attempt + 1,
                        "buffer_ref_source": "FABRIC_SYNCHRONIZED_BUNDLE",
                        "copied_at_us": copied_at_us,
                        "rgb_frame_number": int(rgb_ref.get("frame_number") or -1),
                        "aligned_depth_frame_number": int(
                            depth_ref.get("frame_number") or -1
                        ),
                        "synchronization_delta_us": (
                            self._reference_timestamp(depth_ref)
                            - self._reference_timestamp(rgb_ref)
                        ),
                    },
                },
            )
        raise RuntimeError(
            "camera BufferRef remained unavailable after "
            f"{attempts} fresh-bundle attempts: {last_error}"
        )

    @staticmethod
    def _decode_rgb(payload: bytes, ref: dict[str, Any]) -> np.ndarray:
        width, height = int(ref["width"]), int(ref["height"])
        format_name = str(ref.get("format_name") or "").upper()
        if format_name in {"MJPG", "MJPEG", "JPEG", "JPG"}:
            return np.asarray(Image.open(BytesIO(payload)).convert("RGB"), dtype=np.uint8)
        channels = 4 if format_name in {"RGBA", "BGRA"} else 3
        raw = np.frombuffer(payload, np.uint8).reshape(height, width, channels)
        if format_name == "RGB":
            return raw.copy()
        if format_name == "BGR":
            return raw[:, :, ::-1].copy()
        if format_name == "RGBA":
            return raw[:, :, :3].copy()
        if format_name == "BGRA":
            return raw[:, :, [2, 1, 0]].copy()
        raise RuntimeError(f"unsupported RGB format {format_name!r}")

    @staticmethod
    def _decode_depth(payload: bytes, ref: dict[str, Any]) -> np.ndarray:
        width, height = int(ref["width"]), int(ref["height"])
        format_name = str(ref.get("format_name") or "").upper()
        if format_name not in {"Y16", "DEPTH16", "Z16"}:
            raise RuntimeError(f"unsupported aligned depth format {format_name!r}")
        values = np.frombuffer(payload, dtype="<u2", count=width * height)
        if values.size != width * height:
            raise RuntimeError("aligned depth payload is shorter than declared")
        scale_mm = float(ref.get("depth_value_scale_mm") or 1.0)
        return values.reshape(height, width).astype(np.float32) * scale_mm / 1000.0

    @staticmethod
    def _reference_timestamp(ref: dict[str, Any]) -> int:
        return int(
            ref.get("global_timestamp_us")
            or ref.get("system_timestamp_us")
            or ref.get("device_timestamp_us")
            or 0
        )
