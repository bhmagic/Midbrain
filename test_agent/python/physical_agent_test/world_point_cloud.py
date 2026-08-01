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
WORLD_CONVENTION_ID = "MIDBRAIN_X_FORWARD_Y_LEFT_Z_UP_V2"
CAMERA_OPTICAL_CONVENTION_ID = (
    "CAMERA_OPTICAL_X_RIGHT_Y_DOWN_Z_FORWARD_V1"
)
CALIBRATION_ACTIVATION_STREAM = "manager.workcell_calibration.activation"
LIVE_VIO_CAMERA_AUTHORITY = "LIVE_VIO_CAMERA_POSE"
REVIEWED_STATIONARY_CAMERA_AUTHORITY = "REVIEWED_STATIONARY_CAMERA"
UNRESOLVED_TRANSFORM_AUTHORITY = "UNRESOLVED"


@dataclass
class PointChunk:
    created_monotonic: float
    points_xyzrgb: np.ndarray


@dataclass(frozen=True)
class ReviewedStationaryCameraTransform:
    vio_from_camera: np.ndarray
    calibration_revision: str
    activation_id: str


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
        self.transform_authority = UNRESOLVED_TRANSFORM_AUTHORITY
        self.calibration_revision: str | None = None
        self.calibration_activation_id: str | None = None

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
                self.transform_authority = UNRESOLVED_TRANSFORM_AUTHORITY
                self.calibration_revision = None
                self.calibration_activation_id = None
            self.suspended = False
            self.capture_state = "WAITING_FOR_NEW_SESSION_FRAME"
            self.capture_reason = "new VIO epoch accepted; waiting for the first tracked RGB-D frame"
        await self.start()

    async def resume_follow_latest(self) -> None:
        async with self.capture_lock:
            self._close_reader()
            async with self.lock:
                self.chunks.clear()
                self.last_frame_number = -1
                self.session_epoch = None
                self.world_frame = None
                self.last_success_monotonic = None
                self.transform_authority = UNRESOLVED_TRANSFORM_AUTHORITY
                self.calibration_revision = None
                self.calibration_activation_id = None
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
                "transform_authority": self.transform_authority,
                "calibration_revision": self.calibration_revision,
                "calibration_activation_id": self.calibration_activation_id,
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
                activation_observation,
            ) = await asyncio.gather(
                self.fabric.latest_optional("camera.rgbd.bundle"),
                self.fabric.latest_optional("camera.calibration"),
                self.fabric.latest_optional("localization.body.pose"),
                self.fabric.latest_optional("localization.vio.status"),
                self.fabric.latest_optional(CALIBRATION_ACTIVATION_STREAM),
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
            if vio_data.get("convention_id") != WORLD_CONVENTION_ID:
                self.capture_state = "PAUSED_SPATIAL_CONVENTION_MISMATCH"
                self.capture_reason = (
                    "VIO epoch is not MIDBRAIN_X_FORWARD_Y_LEFT_Z_UP_V2"
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

            reviewed_camera_transform = _reviewed_stationary_camera_transform(
                activation_observation,
                session_epoch=str(session_epoch),
                vio_world_frame=str(world_frame),
            )
            if reviewed_camera_transform is None:
                await self._set_transform_authority(
                    LIVE_VIO_CAMERA_AUTHORITY,
                    calibration_revision=None,
                    activation_id=None,
                )
            else:
                await self._set_transform_authority(
                    REVIEWED_STATIONARY_CAMERA_AUTHORITY,
                    calibration_revision=reviewed_camera_transform.calibration_revision,
                    activation_id=reviewed_camera_transform.activation_id,
                )

            bundle = bundle_observation.get("data") or {}
            bundle_conventions = bundle.get("coordinate_conventions") or {}
            if (
                bundle_conventions.get("rgb")
                != CAMERA_OPTICAL_CONVENTION_ID
                or bundle_conventions.get("aligned_depth")
                != CAMERA_OPTICAL_CONVENTION_ID
            ):
                self.capture_state = (
                    "PAUSED_CAMERA_CONVENTION_UNDECLARED"
                )
                self.capture_reason = (
                    "RGB-D bundle does not explicitly declare native camera "
                    "optical X-right/Y-down/Z-forward coordinates"
                )
                return
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
            if reviewed_camera_transform is None:
                transform: dict[str, Any] | np.ndarray = await self.fabric.transform(
                    from_frame=COLOR_FRAME,
                    to_frame=str(world_frame),
                    at_us=timestamp_us,
                    max_extrapolation_us=750_000,
                    session_epoch=str(session_epoch),
                )
            else:
                transform = reviewed_camera_transform.vio_from_camera
            points = self._make_world_points(rgb, depth_m, intrinsics, transform)
            self.last_frame_number = frame_number
            if points.size == 0:
                return
            async with self.lock:
                now = time.monotonic()
                self.chunks.append(PointChunk(now, points))
                self.last_success_monotonic = now
                self.capture_state = "CAPTURING"
                if (
                    self.transform_authority
                    == REVIEWED_STATIONARY_CAMERA_AUTHORITY
                ):
                    self.capture_reason = (
                        "using the exact Manager-reviewed stationary camera reference"
                    )
                else:
                    self.capture_reason = (
                        "using the timestamped live VIO camera pose"
                    )
                self._purge_locked(now)
                self._enforce_limit_locked()

    async def _set_transform_authority(
        self,
        authority: str,
        *,
        calibration_revision: str | None,
        activation_id: str | None,
    ) -> None:
        current_key = (
            self.transform_authority,
            self.calibration_revision,
            self.calibration_activation_id,
        )
        next_key = (authority, calibration_revision, activation_id)
        if current_key == next_key:
            return
        async with self.lock:
            self.chunks.clear()
            self.last_frame_number = -1
            self.last_success_monotonic = None
            self.transform_authority = authority
            self.calibration_revision = calibration_revision
            self.calibration_activation_id = activation_id

    def _make_world_points(
        self,
        rgb: np.ndarray,
        depth_m: np.ndarray,
        intrinsics: dict[str, Any],
        transform: dict[str, Any] | np.ndarray,
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
        camera_system_points = np.column_stack(
            (
                (u - cx) * z_valid / fx,
                (v - cy) * z_valid / fy,
                z_valid,
                np.ones_like(z_valid),
            )
        )
        if isinstance(transform, np.ndarray):
            world_from_camera = _validated_transform_matrix(
                transform,
                "camera projection transform",
            )
        else:
            world_from_camera = _matrix_from_transform(
                transform,
                "camera projection transform",
            )
        world_points = (
            world_from_camera @ camera_system_points.T
        ).T[:, :3]
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


def _matrix_from_transform(value: Any, label: str) -> np.ndarray:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    try:
        translation = np.asarray(value["translation_m"], dtype=np.float64)
        quaternion = np.asarray(value["rotation_xyzw"], dtype=np.float64)
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"{label} is not a valid rigid transform") from error
    if translation.shape != (3,) or not np.all(np.isfinite(translation)):
        raise ValueError(f"{label}.translation_m must contain three finite values")
    if quaternion.shape != (4,) or not np.all(np.isfinite(quaternion)):
        raise ValueError(f"{label}.rotation_xyzw must contain four finite values")
    quaternion_norm = float(np.linalg.norm(quaternion))
    if quaternion_norm <= 1e-9:
        raise ValueError(f"{label}.rotation_xyzw must be non-zero")
    matrix = np.eye(4, dtype=np.float64)
    matrix[:3, :3] = quaternion_xyzw_to_matrix(quaternion)
    matrix[:3, 3] = translation
    return matrix


def _validated_transform_matrix(value: Any, label: str) -> np.ndarray:
    matrix = np.asarray(value, dtype=np.float64)
    if matrix.shape != (4, 4) or not np.all(np.isfinite(matrix)):
        raise ValueError(f"{label} must be a finite 4x4 matrix")
    if not np.allclose(matrix[3], [0.0, 0.0, 0.0, 1.0], atol=1e-9):
        raise ValueError(f"{label} must have a rigid homogeneous final row")
    rotation = matrix[:3, :3]
    if not np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-6):
        raise ValueError(f"{label} rotation must be orthonormal")
    if not np.isclose(np.linalg.det(rotation), 1.0, atol=1e-6):
        raise ValueError(f"{label} rotation must be right-handed")
    return matrix


def _reviewed_stationary_camera_transform(
    observation: Any,
    *,
    session_epoch: str,
    vio_world_frame: str,
    now_us: int | None = None,
) -> ReviewedStationaryCameraTransform | None:
    if not isinstance(observation, dict):
        return None
    if observation.get("valid") is not True:
        return None
    if observation.get("stream") != CALIBRATION_ACTIVATION_STREAM:
        return None
    if observation.get("schema") != "physical_agent.workcell_calibration_activation":
        return None
    if observation.get("provider_id") != "manager.workcell_calibration":
        return None
    data = observation.get("data")
    if not isinstance(data, dict):
        return None
    if data.get("state") != "ACTIVE" or data.get("motion_usable") is not True:
        return None
    if str(data.get("session_epoch") or "") != session_epoch:
        return None
    if str(data.get("vio_world_frame") or "") != vio_world_frame:
        return None
    if data.get("camera_frame") != COLOR_FRAME:
        return None
    if data.get("convention_id") != WORLD_CONVENTION_ID:
        return None
    if data.get("camera_optical_convention_id") != CAMERA_OPTICAL_CONVENTION_ID:
        return None
    expiry = data.get("expires_at_us")
    if isinstance(expiry, bool) or not isinstance(expiry, int):
        return None
    current_time_us = time.time_ns() // 1_000 if now_us is None else int(now_us)
    if expiry <= current_time_us:
        return None
    calibration_revision = str(data.get("calibration_revision") or "")
    activation_id = str(data.get("activation_id") or "")
    if not calibration_revision or not activation_id:
        return None
    if str(observation.get("calibration_revision") or "") != calibration_revision:
        return None
    observation_expiry = observation.get("expires_at_us")
    if (
        isinstance(observation_expiry, bool)
        or not isinstance(observation_expiry, int)
        or observation_expiry <= current_time_us
    ):
        return None
    transforms = data.get("transforms")
    if not isinstance(transforms, dict):
        return None
    try:
        world_from_camera = _matrix_from_transform(
            transforms.get("world_from_camera"),
            "activation.transforms.world_from_camera",
        )
        world_from_vio = _matrix_from_transform(
            transforms.get("world_from_vio"),
            "activation.transforms.world_from_vio",
        )
        vio_from_camera = np.linalg.inv(world_from_vio) @ world_from_camera
        vio_from_camera = _validated_transform_matrix(
            vio_from_camera,
            "reviewed VIO-from-camera transform",
        )
    except (ValueError, np.linalg.LinAlgError):
        return None
    return ReviewedStationaryCameraTransform(
        vio_from_camera=vio_from_camera,
        calibration_revision=calibration_revision,
        activation_id=activation_id,
    )
