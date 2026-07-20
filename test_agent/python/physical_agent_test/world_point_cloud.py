from __future__ import annotations

import asyncio
import struct
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np
from PIL import Image

from .fabric_client import FabricClient
from orbbec_femto_provider.shared_memory_access import CameraSharedMemory

COLOR_FRAME = "femto_bolt_color_optical_frame"


@dataclass
class PointChunk:
    created_monotonic: float
    points_xyzrgb: np.ndarray


class WorldPointCloudAccumulator:
    """Accumulate RGB-D points in the current VIO world frame for ten seconds."""

    def __init__(
        self,
        fabric: FabricClient,
        *,
        retention_s: float,
        sample_stride: int,
        update_hz: float,
        max_points: int,
    ):
        self.fabric = fabric
        self.retention_s = retention_s
        self.sample_stride = max(2, sample_stride)
        self.update_period_s = 1.0 / max(0.5, update_hz)
        self.max_points = max(10_000, max_points)
        self.chunks: deque[PointChunk] = deque()
        self.lock = asyncio.Lock()
        self.capture_lock = asyncio.Lock()
        self.stop_event = asyncio.Event()
        self.task: asyncio.Task[None] | None = None
        self.last_frame_number = -1
        self.reader: CameraSharedMemory | None = None
        self.mapping_name: str | None = None
        self.last_error: str | None = None
        self.last_transient_error: str | None = None
        self.dropped_buffer_frames = 0
        self.last_success_monotonic: float | None = None
        self.world_frame: str | None = None
        self.session_epoch: str | None = None
        self.suspended = False
        self.capture_state = "NOT_STARTED"
        self.capture_reason = "point-cloud task has not started"
        self.last_vio_tracking_state: str | None = None
        self.last_vio_message: str | None = None

    async def start(self) -> None:
        if self.task is not None and self.task.done():
            self.task = None
        if self.task is None:
            self.stop_event.clear()
            self.capture_state = "STARTING"
            self.capture_reason = "starting point-cloud accumulator"
            self.task = asyncio.create_task(self._run(), name="world-point-cloud")

    async def stop(self) -> None:
        self.stop_event.set()
        if self.task is not None:
            await self.task
            self.task = None
        self._close_reader()

    async def clear(self) -> None:
        async with self.capture_lock:
            async with self.lock:
                self.chunks.clear()
                self.last_frame_number = -1
                self.last_error = None

    async def begin_reinitialization(self) -> None:
        async with self.capture_lock:
            self.suspended = True
            self.capture_state = "SUSPENDED_FOR_REINITIALIZATION"
            self.capture_reason = "manual origin reset is in progress"
            self._close_reader()
            async with self.lock:
                self.last_error = None

    async def switch_session(self, *, session_epoch: str, world_frame: str) -> None:
        async with self.capture_lock:
            self._close_reader()
            async with self.lock:
                self.chunks.clear()
                self.last_frame_number = -1
                self.session_epoch = str(session_epoch)
                self.world_frame = str(world_frame)
                self.last_error = None
                self.last_transient_error = None
                self.last_success_monotonic = None
            self.suspended = False
            self.capture_state = "WAITING_FOR_NEW_SESSION_FRAME"
            self.capture_reason = "new VIO epoch accepted; waiting for the first tracked RGB-D frame"
        await self.start()

    async def resume_follow_latest(self) -> None:
        async with self.capture_lock:
            self._close_reader()
            async with self.lock:
                self.last_frame_number = -1
                self.session_epoch = None
                self.world_frame = None
            self.suspended = False
            self.capture_state = "FOLLOWING_LATEST_SESSION"
            self.capture_reason = "reset failed; following the latest published VIO session"
        await self.start()

    async def wait_for_points(self, *, session_epoch: str, timeout_s: float) -> bool:
        await self.start()
        deadline = asyncio.get_running_loop().time() + max(0.1, timeout_s)
        while asyncio.get_running_loop().time() < deadline:
            async with self.lock:
                if self.session_epoch == session_epoch and self.chunks:
                    return True
            await asyncio.sleep(0.05)
        return False

    async def status(self) -> dict[str, Any]:
        now = time.monotonic()
        async with self.lock:
            self._purge_locked(now)
            point_count = sum(chunk.points_xyzrgb.shape[0] for chunk in self.chunks)
            seconds_since_last_chunk = (
                None
                if self.last_success_monotonic is None
                else max(0.0, now - self.last_success_monotonic)
            )
            return {
                "point_count": point_count,
                "chunk_count": len(self.chunks),
                "retention_s": self.retention_s,
                "world_frame": self.world_frame,
                "session_epoch": self.session_epoch,
                "suspended": self.suspended,
                "capture_state": self.capture_state,
                "capture_reason": self.capture_reason,
                "task_running": self.task is not None and not self.task.done(),
                "last_vio_tracking_state": self.last_vio_tracking_state,
                "last_vio_message": self.last_vio_message,
                "dropped_buffer_frames": self.dropped_buffer_frames,
                "last_transient_error": self.last_transient_error,
                "seconds_since_last_chunk": seconds_since_last_chunk,
                "last_error": self.last_error,
            }

    async def snapshot_binary(self) -> bytes:
        now = time.monotonic()
        async with self.lock:
            self._purge_locked(now)
            arrays: list[np.ndarray] = []
            for chunk in self.chunks:
                age = np.full(
                    (chunk.points_xyzrgb.shape[0], 1),
                    now - chunk.created_monotonic,
                    dtype=np.float32,
                )
                arrays.append(np.concatenate((chunk.points_xyzrgb, age), axis=1))
            if arrays:
                records = np.concatenate(arrays, axis=0).astype("<f4", copy=False)
            else:
                records = np.empty((0, 7), dtype="<f4")
        return struct.pack("<I", records.shape[0]) + records.tobytes(order="C")

    async def _run(self) -> None:
        self.capture_state = "RUNNING"
        self.capture_reason = "capture loop is running"
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                await self._capture_once()
                self.last_error = None
            except Exception as error:
                message = str(error)
                if self._is_transient_buffer_error(message):
                    self.dropped_buffer_frames += 1
                    self.last_transient_error = message
                    self.last_error = None
                else:
                    self.last_error = message
            elapsed = time.monotonic() - started
            try:
                await asyncio.wait_for(
                    self.stop_event.wait(),
                    timeout=max(0.01, self.update_period_s - elapsed),
                )
            except TimeoutError:
                pass

    async def _capture_once(self) -> None:
        if self.suspended:
            return
        async with self.capture_lock:
            if self.suspended:
                return
            (
                bundle_observation,
                calibration_observation,
                pose_observation,
                vio_observation,
            ) = await asyncio.gather(
                self.fabric.latest_optional("camera.rgbd.bundle"),
                self.fabric.latest_optional("camera.calibration"),
                self.fabric.latest_optional("localization.body.pose"),
                self.fabric.latest_optional("localization.vio.status"),
            )
            if not bundle_observation or not calibration_observation or not pose_observation:
                self.capture_state = "WAITING_FOR_INPUTS"
                self.capture_reason = "waiting for RGB-D bundle, calibration, and body pose"
                return
            pose = pose_observation.get("data") or {}
            world_frame = pose.get("world_frame")
            session_epoch = pose.get("session_epoch")
            if not world_frame or not session_epoch:
                self.capture_state = "WAITING_FOR_POSE_SESSION"
                self.capture_reason = "body pose does not yet contain a VIO world frame and epoch"
                return
            vio_data = (vio_observation or {}).get("data") or {}
            self.last_vio_tracking_state = str(vio_data.get("tracking_state") or "UNKNOWN")
            self.last_vio_message = (
                None if vio_data.get("message") is None else str(vio_data.get("message"))
            )
            if (
                vio_data.get("tracking_state") != "TRACKING"
                or str(vio_data.get("session_epoch") or "") != str(session_epoch)
            ):
                self.capture_state = "PAUSED_UNTIL_VISUAL_TRACKING"
                self.capture_reason = (
                    f"VIO {self.last_vio_tracking_state}; "
                    + (self.last_vio_message or "waiting for visual tracking in the active epoch")
                )
                return
            if self.session_epoch != str(session_epoch):
                self._close_reader()
                async with self.lock:
                    self.chunks.clear()
                    self.last_frame_number = -1
                    self.world_frame = str(world_frame)
                    self.session_epoch = str(session_epoch)
                    self.last_error = None
            else:
                self.world_frame = str(world_frame)

            bundle = bundle_observation.get("data") or {}
            rgb_reference = bundle.get("rgb")
            depth_reference = bundle.get("depth_aligned_to_rgb")
            if not isinstance(rgb_reference, dict) or not isinstance(depth_reference, dict):
                self.capture_state = "WAITING_FOR_ALIGNED_RGBD"
                self.capture_reason = "RGB-D bundle does not contain aligned RGB and depth references"
                return
            frame_number = int(rgb_reference.get("frame_number", -1))
            if frame_number <= self.last_frame_number:
                self.capture_state = "WAITING_FOR_NEW_FRAME"
                self.capture_reason = "waiting for a newer RGB-D frame number"
                return
            mapping_name = str(rgb_reference.get("mapping_name") or "")
            self._ensure_reader(mapping_name)
            rgb = self._read_rgb(rgb_reference)
            depth_m = self._read_depth_m(depth_reference)
            calibration = calibration_observation.get("data") or {}
            intrinsics = calibration.get("rgb_intrinsic") or {}
            timestamp_us = self._reference_timestamp(rgb_reference)
            transform = await self.fabric.transform(
                from_frame=COLOR_FRAME,
                to_frame=str(world_frame),
                at_us=timestamp_us,
                max_extrapolation_us=750_000,
                session_epoch=str(session_epoch),
            )
            points = self._make_world_points(rgb, depth_m, intrinsics, transform)
            self.last_frame_number = frame_number
            if points.size == 0:
                return
            async with self.lock:
                now = time.monotonic()
                self.chunks.append(PointChunk(now, points))
                self.last_success_monotonic = now
                self.capture_state = "CAPTURING"
                self.capture_reason = "visual tracking and world transform are valid"
                self._purge_locked(now)
                self._enforce_limit_locked()

    def _make_world_points(
        self,
        rgb: np.ndarray,
        depth_m: np.ndarray,
        intrinsics: dict[str, Any],
        transform: dict[str, Any],
    ) -> np.ndarray:
        fx = float(intrinsics.get("fx", 0.0))
        fy = float(intrinsics.get("fy", 0.0))
        cx = float(intrinsics.get("cx", 0.0))
        cy = float(intrinsics.get("cy", 0.0))
        if fx <= 0.0 or fy <= 0.0:
            raise RuntimeError("RGB intrinsics are invalid for point-cloud projection")
        height = min(rgb.shape[0], depth_m.shape[0])
        width = min(rgb.shape[1], depth_m.shape[1])
        rows = np.arange(0, height, self.sample_stride)
        columns = np.arange(0, width, self.sample_stride)
        grid_x, grid_y = np.meshgrid(columns, rows)
        z = depth_m[grid_y, grid_x]
        valid = np.isfinite(z) & (z >= 0.2) & (z <= 8.0)
        if not np.any(valid):
            return np.empty((0, 6), dtype=np.float32)
        u = grid_x[valid].astype(np.float64)
        v = grid_y[valid].astype(np.float64)
        z_valid = z[valid].astype(np.float64)
        camera_points = np.column_stack(
            (
                (u - cx) * z_valid / fx,
                (v - cy) * z_valid / fy,
                z_valid,
                np.ones_like(z_valid),
            )
        )
        rotation = quaternion_xyzw_to_matrix(transform["rotation_xyzw"])
        world_from_camera = np.eye(4, dtype=np.float64)
        world_from_camera[:3, :3] = rotation
        world_from_camera[:3, 3] = np.asarray(transform["translation_m"], dtype=np.float64)
        world_points = (world_from_camera @ camera_points.T).T[:, :3]
        colors = rgb[grid_y[valid], grid_x[valid]].astype(np.float32) / 255.0
        return np.concatenate((world_points.astype(np.float32), colors), axis=1)

    def _purge_locked(self, now: float) -> None:
        while self.chunks and now - self.chunks[0].created_monotonic > self.retention_s:
            self.chunks.popleft()

    def _enforce_limit_locked(self) -> None:
        total = sum(chunk.points_xyzrgb.shape[0] for chunk in self.chunks)
        while len(self.chunks) > 1 and total > self.max_points:
            total -= self.chunks.popleft().points_xyzrgb.shape[0]
        if self.chunks and total > self.max_points:
            chunk = self.chunks[-1]
            step = max(1, int(np.ceil(chunk.points_xyzrgb.shape[0] / self.max_points)))
            chunk.points_xyzrgb = chunk.points_xyzrgb[::step][: self.max_points]

    def _ensure_reader(self, mapping_name: str) -> None:
        if not mapping_name:
            raise RuntimeError("RGB BufferRef is missing mapping_name")
        if self.reader is not None and self.mapping_name == mapping_name:
            return
        self._close_reader()
        self.reader = CameraSharedMemory(mapping_name).open()
        self.mapping_name = mapping_name

    def _close_reader(self) -> None:
        if self.reader is not None:
            self.reader.close()
            self.reader = None
        self.mapping_name = None

    def _read_rgb(self, reference: dict[str, Any]) -> np.ndarray:
        if self.reader is None:
            raise RuntimeError("shared memory is not open")
        payload = self.reader.read_ref(reference)
        format_name = str(reference.get("format_name", "")).upper()
        width = int(reference["width"])
        height = int(reference["height"])
        if format_name in {"MJPG", "MJPEG", "JPEG", "JPG"}:
            decoded = cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_COLOR)
            if decoded is None:
                raise RuntimeError("could not decode JPEG RGB frame")
            return cv2.cvtColor(decoded, cv2.COLOR_BGR2RGB)
        if format_name == "RGB":
            return np.frombuffer(payload, np.uint8).reshape(height, width, 3).copy()
        if format_name == "BGR":
            bgr = np.frombuffer(payload, np.uint8).reshape(height, width, 3)
            return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        if format_name == "RGBA":
            return np.asarray(Image.frombytes("RGBA", (width, height), payload).convert("RGB"))
        if format_name == "BGRA":
            image = Image.frombytes("RGBA", (width, height), payload, "raw", "BGRA")
            return np.asarray(image.convert("RGB"))
        raise RuntimeError(f"unsupported RGB format: {format_name or 'unknown'}")

    def _read_depth_m(self, reference: dict[str, Any]) -> np.ndarray:
        if self.reader is None:
            raise RuntimeError("shared memory is not open")
        payload = self.reader.read_ref(reference)
        format_name = str(reference.get("format_name", "")).upper()
        if format_name not in {"Y16", "DEPTH16", "Z16"}:
            raise RuntimeError(f"unsupported aligned depth format: {format_name or 'unknown'}")
        width = int(reference["width"])
        height = int(reference["height"])
        expected = width * height
        values = np.frombuffer(payload, dtype="<u2", count=expected)
        if values.size != expected:
            raise RuntimeError("aligned depth payload is shorter than declared")
        scale_mm = float(reference.get("depth_value_scale_mm") or 1.0)
        return values.reshape(height, width).astype(np.float32) * (scale_mm / 1000.0)

    @staticmethod
    def _reference_timestamp(reference: dict[str, Any]) -> int:
        return int(
            reference.get("global_timestamp_us")
            or reference.get("system_timestamp_us")
            or reference.get("device_timestamp_us")
            or 0
        )

    @staticmethod
    def _is_transient_buffer_error(message: str) -> bool:
        normalized = message.lower()
        return "bufferref" in normalized and (
            "expired" in normalized or "slot was recycled" in normalized
        )


def quaternion_xyzw_to_matrix(value: Any) -> np.ndarray:
    x, y, z, w = (float(item) for item in value)
    norm = (x * x + y * y + z * z + w * w) ** 0.5
    if norm <= 1e-12:
        return np.eye(3, dtype=np.float64)
    x, y, z, w = x / norm, y / norm, z / norm, w / norm
    return np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ],
        dtype=np.float64,
    )
