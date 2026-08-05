from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any

import cv2
import numpy as np

from .clients import FabricClient


@dataclass(frozen=True)
class RgbdFrame:
    rgb: np.ndarray
    depth_m: np.ndarray
    intrinsics: dict[str, Any]
    observed_at_us: int
    frame_number: int
    camera_frame: str
    session_epoch: str
    calibration_revision: str
    camera_provider_id: str
    camera_provider_instance_id: str
    camera_boot_id: str
    source_observations: dict[str, Any]


class RgbdCapture:
    """Copy finite-retention aligned RGB-D references immediately."""

    def __init__(self, fabric: FabricClient) -> None:
        self.fabric = fabric

    @staticmethod
    def _timestamp(reference: dict[str, Any], observation: dict[str, Any]) -> int:
        return int(
            reference.get("global_timestamp_us")
            or reference.get("system_timestamp_us")
            or reference.get("device_timestamp_us")
            or observation.get("observed_at_us")
            or 0
        )

    @staticmethod
    def _read_rgb(reader: Any, reference: dict[str, Any]) -> np.ndarray:
        payload = reader.read_ref(reference)
        width, height = int(reference["width"]), int(reference["height"])
        format_name = str(reference.get("format_name") or "").upper()
        if format_name in {"MJPG", "MJPEG", "JPEG", "JPG"}:
            bgr = cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_COLOR)
            if bgr is None:
                raise RuntimeError("camera JPEG frame could not be decoded")
            return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        if format_name == "RGB":
            return np.frombuffer(payload, np.uint8).reshape(height, width, 3).copy()
        if format_name == "BGR":
            bgr = np.frombuffer(payload, np.uint8).reshape(height, width, 3)
            return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        if format_name in {"RGBA", "BGRA"}:
            channels = np.frombuffer(payload, np.uint8).reshape(height, width, 4)
            code = cv2.COLOR_RGBA2RGB if format_name == "RGBA" else cv2.COLOR_BGRA2RGB
            return cv2.cvtColor(channels, code)
        raise RuntimeError(f"unsupported RGB format {format_name!r}")

    @staticmethod
    def _read_depth(reader: Any, reference: dict[str, Any]) -> np.ndarray:
        payload = reader.read_ref(reference)
        width, height = int(reference["width"]), int(reference["height"])
        format_name = str(reference.get("format_name") or "").upper()
        if format_name not in {"Y16", "DEPTH16", "Z16"}:
            raise RuntimeError(f"unsupported aligned depth format {format_name!r}")
        values = np.frombuffer(payload, dtype="<u2", count=width * height)
        if values.size != width * height:
            raise RuntimeError("aligned depth payload is shorter than declared")
        scale_mm = float(reference.get("depth_value_scale_mm") or 1.0)
        return values.reshape(height, width).astype(np.float32) * scale_mm / 1000.0

    def capture(self, *, attempts: int = 6) -> RgbdFrame:
        from orbbec_femto_provider.shared_memory_access import CameraSharedMemory

        calibration_observation = self.fabric.latest_optional("camera.calibration")
        if not calibration_observation:
            raise RuntimeError("camera calibration is unavailable")
        last_error: Exception | None = None
        for attempt in range(max(1, int(attempts))):
            bundle_observation = self.fabric.latest_optional("camera.rgbd.bundle")
            if not bundle_observation:
                raise RuntimeError("aligned RGB-D bundle is unavailable")
            data = bundle_observation.get("data")
            data = data if isinstance(data, dict) else {}
            rgb_ref = data.get("rgb")
            depth_ref = data.get("depth_aligned_to_rgb")
            if not isinstance(rgb_ref, dict) or not isinstance(depth_ref, dict):
                raise RuntimeError("RGB-D bundle lacks aligned BufferRefs")
            mapping_name = str(rgb_ref.get("mapping_name") or "").strip()
            if not mapping_name:
                raise RuntimeError("RGB BufferRef has no mapping_name")
            reader = CameraSharedMemory(mapping_name).open()
            try:
                rgb = self._read_rgb(reader, rgb_ref)
                depth = self._read_depth(reader, depth_ref)
            except Exception as error:
                last_error = error
                message = str(error).lower()
                transient = (
                    "expired" in message
                    or "slot was recycled" in message
                    or "consistent shared-memory payload" in message
                )
                if not transient or attempt + 1 >= attempts:
                    raise
                time.sleep(0.02)
                continue
            finally:
                reader.close()

            # The tracker publishes only in the reviewed mounted arm-base
            # frame. Its persistent-map identity therefore follows the
            # canonical camera/calibration and reviewed transform, not a
            # transient Local VIO process or epoch.
            epoch = "DIRECT_CAMERA_TO_ARM_BASE_NO_VIO_EPOCH"
            calibration = calibration_observation.get("data")
            calibration = calibration if isinstance(calibration, dict) else {}
            intrinsics = calibration.get("rgb_intrinsic")
            if not isinstance(intrinsics, dict):
                raise RuntimeError("RGB intrinsics are unavailable")
            coordinate_frames = data.get("coordinate_frames")
            coordinate_frames = (
                coordinate_frames if isinstance(coordinate_frames, dict) else {}
            )
            camera_frame = str(
                coordinate_frames.get("aligned_depth")
                or coordinate_frames.get("rgb")
                or "femto_bolt_color_optical_frame"
            ).strip()
            timestamp = self._timestamp(rgb_ref, bundle_observation)
            if timestamp <= 0:
                raise RuntimeError("RGB-D capture has no usable timestamp")
            return RgbdFrame(
                rgb=rgb,
                depth_m=depth,
                intrinsics=dict(intrinsics),
                observed_at_us=timestamp,
                frame_number=int(rgb_ref.get("frame_number") or -1),
                camera_frame=camera_frame,
                session_epoch=epoch,
                calibration_revision=str(
                    calibration.get("calibration_revision")
                    or bundle_observation.get("calibration_revision")
                    or data.get("calibration_revision")
                    or ""
                ),
                camera_provider_id=str(bundle_observation.get("provider_id") or ""),
                camera_provider_instance_id=str(
                    bundle_observation.get("provider_instance_id") or ""
                ),
                camera_boot_id=str(bundle_observation.get("boot_id") or ""),
                source_observations={
                    "bundle": bundle_observation,
                    "calibration": calibration_observation,
                    "mounted_transform_context": {
                        "target_frame": "rebot_arm_base",
                        "vio_required": False,
                    },
                    "copy_attempt": attempt + 1,
                },
            )
        raise RuntimeError(f"RGB-D BufferRef copy failed: {last_error}")
