from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import cv2
import httpx
import numpy as np
from orbbec_femto_provider.shared_memory_access import CameraSharedMemory

from .clients import FabricClient


@dataclass(frozen=True)
class RgbdFrame:
    rgb: np.ndarray
    depth_m: np.ndarray
    intrinsics: dict[str, Any]
    timestamp_us: int
    frame_number: int
    camera_frame: str
    session_epoch: str
    calibration_revision: str | None
    observations: dict[str, Any]


class RgbdCapture:
    def __init__(self, fabric: FabricClient, camera_frame: str):
        self.fabric = fabric
        self.camera_frame = camera_frame

    async def capture(
        self,
        attempts: int = 8,
        retry_delay_s: float = 0.04,
        *,
        require_vio: bool = True,
    ) -> RgbdFrame:
        calibration_observation = await self._latest_optional_with_retry(
            "camera.calibration"
        )
        pose_observation = (
            await self._latest_optional_with_retry(
                "localization.body.pose"
            )
            if require_vio
            else None
        )
        vio_observation = (
            await self._latest_optional_with_retry(
                "localization.vio.status"
            )
            if require_vio
            else None
        )
        if not calibration_observation:
            raise RuntimeError("camera calibration is unavailable")
        if require_vio and not pose_observation:
            raise RuntimeError("VIO body pose is unavailable")

        last_error: Exception | None = None
        for attempt in range(attempts):
            bundle_observation = await self._latest_optional_with_retry(
                "camera.rgbd.bundle"
            )
            if not bundle_observation:
                raise RuntimeError("RGB-D bundle is unavailable")
            bundle = bundle_observation.get("data") or {}
            rgb_ref = bundle.get("rgb")
            depth_ref = bundle.get("depth_aligned_to_rgb")
            if not isinstance(rgb_ref, dict) or not isinstance(depth_ref, dict):
                raise RuntimeError("RGB-D bundle has no aligned RGB and depth BufferRefs")
            mapping_name = str(rgb_ref.get("mapping_name") or "")
            if not mapping_name:
                raise RuntimeError("RGB BufferRef has no shared-memory mapping name")
            reader = CameraSharedMemory(mapping_name).open()
            try:
                rgb = self._read_rgb(reader, rgb_ref)
                depth = self._read_depth(reader, depth_ref)
            except Exception as error:
                if not self._is_transient_buffer_error(error):
                    raise
                last_error = error
                if attempt + 1 < attempts:
                    await asyncio.sleep(retry_delay_s)
                continue
            finally:
                reader.close()
            calibration = calibration_observation.get("data") or {}
            intrinsics = calibration.get("rgb_intrinsic") or {}
            if float(intrinsics.get("fx") or 0) <= 0:
                raise RuntimeError("camera RGB intrinsics are invalid")
            pose = (pose_observation or {}).get("data") or {}
            vio = (vio_observation or {}).get("data") or {}
            epoch = str(pose.get("session_epoch") or vio.get("session_epoch") or "")
            if require_vio and not epoch:
                raise RuntimeError("VIO session epoch is unavailable")
            timestamp_us = int(
                rgb_ref.get("global_timestamp_us")
                or rgb_ref.get("system_timestamp_us")
                or rgb_ref.get("device_timestamp_us")
                or bundle_observation.get("observed_at_us")
                or 0
            )
            return RgbdFrame(
                rgb=rgb,
                depth_m=depth,
                intrinsics=intrinsics,
                timestamp_us=timestamp_us,
                frame_number=int(rgb_ref.get("frame_number") or -1),
                camera_frame=self.camera_frame,
                session_epoch=epoch,
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
                    "calibration": calibration_observation,
                    "body_pose": pose_observation,
                    "vio_status": vio_observation,
                    "copy_attempt": attempt + 1,
                },
            )
        raise RuntimeError(f"camera BufferRef remained unavailable: {last_error}")

    async def _latest_optional_with_retry(
        self,
        stream: str,
        *,
        attempts: int = 3,
        retry_delay_s: float = 0.12,
        timeout_s: float = 5.0,
    ) -> dict[str, Any] | None:
        last_error: BaseException | None = None
        for attempt in range(attempts):
            try:
                return await asyncio.wait_for(
                    self.fabric.latest_optional(stream),
                    timeout=timeout_s,
                )
            except (TimeoutError, httpx.TransportError) as error:
                last_error = error
                if attempt + 1 < attempts:
                    await asyncio.sleep(retry_delay_s)
        error_name = (
            type(last_error).__name__
            if last_error is not None
            else "unknown transport error"
        )
        error_message = str(last_error or "").strip()
        detail = f": {error_message}" if error_message else ""
        raise RuntimeError(
            f"Fabric read for {stream} failed after {attempts} attempts "
            f"({error_name}){detail}"
        ) from last_error

    @staticmethod
    def _is_transient_buffer_error(error: Exception) -> bool:
        message = str(error).lower()
        return (
            "bufferref has expired" in message
            or "slot was recycled" in message
            or "consistent shared-memory payload" in message
        )

    @staticmethod
    def _read_rgb(reader: CameraSharedMemory, ref: dict[str, Any]) -> np.ndarray:
        payload = reader.read_ref(ref)
        width, height = int(ref["width"]), int(ref["height"])
        format_name = str(ref.get("format_name") or "").upper()
        if format_name in {"MJPG", "MJPEG", "JPEG", "JPG"}:
            bgr = cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_COLOR)
            if bgr is None:
                raise RuntimeError("JPEG RGB frame could not be decoded")
            return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        if format_name == "RGB":
            return np.frombuffer(payload, np.uint8).reshape(height, width, 3).copy()
        if format_name == "BGR":
            bgr = np.frombuffer(payload, np.uint8).reshape(height, width, 3)
            return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        raise RuntimeError(f"unsupported RGB format: {format_name or 'unknown'}")

    @staticmethod
    def _read_depth(reader: CameraSharedMemory, ref: dict[str, Any]) -> np.ndarray:
        payload = reader.read_ref(ref)
        width, height = int(ref["width"]), int(ref["height"])
        format_name = str(ref.get("format_name") or "").upper()
        if format_name not in {"Y16", "DEPTH16", "Z16"}:
            raise RuntimeError(f"unsupported aligned depth format: {format_name or 'unknown'}")
        values = np.frombuffer(payload, dtype="<u2", count=width * height)
        if values.size != width * height:
            raise RuntimeError("aligned depth payload is shorter than declared")
        scale_mm = float(ref.get("depth_value_scale_mm") or 1.0)
        return values.reshape(height, width).astype(np.float32) * scale_mm / 1000.0


def encode_rgb_jpeg(rgb: np.ndarray, quality: int = 92) -> bytes:
    ok, encoded = cv2.imencode(
        ".jpg",
        cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR),
        [cv2.IMWRITE_JPEG_QUALITY, int(quality)],
    )
    if not ok:
        raise RuntimeError("could not encode RGB image")
    return encoded.tobytes()


def encode_depth_png(depth_m: np.ndarray) -> bytes:
    valid = depth_m[np.isfinite(depth_m) & (depth_m > 0)]
    if valid.size:
        near, far = float(np.percentile(valid, 2)), float(np.percentile(valid, 98))
        scale = np.clip((far - depth_m) / max(1e-6, far - near), 0, 1)
        gray = (scale * 255).astype(np.uint8)
        gray[~np.isfinite(depth_m) | (depth_m <= 0)] = 0
    else:
        gray = np.zeros(depth_m.shape, np.uint8)
    ok, encoded = cv2.imencode(".png", gray)
    if not ok:
        raise RuntimeError("could not encode depth image")
    return encoded.tobytes()
