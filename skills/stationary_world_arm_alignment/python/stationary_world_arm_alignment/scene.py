from __future__ import annotations

import asyncio
import struct
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

import numpy as np

from .camera import RgbdCapture
from .clients import FabricClient
from .math3d import quaternion_xyzw_to_matrix


@dataclass
class PointChunk:
    created_monotonic: float
    points_xyzrgb: np.ndarray


class WorldPointCloud:
    """Small read-only monitor accumulator derived from Midbrain's test-agent visual path."""

    def __init__(
        self,
        fabric: FabricClient,
        camera_frame: str,
        *,
        stride: int,
        update_hz: float,
        max_points: int,
        retention_s: float = 10.0,
    ):
        self.fabric = fabric
        self.capture = RgbdCapture(fabric, camera_frame)
        self.stride = max(2, stride)
        self.period_s = 1.0 / max(0.25, update_hz)
        self.max_points = max(10_000, max_points)
        self.retention_s = retention_s
        self.chunks: deque[PointChunk] = deque()
        self.lock = asyncio.Lock()
        self.stop_event = asyncio.Event()
        self.task: asyncio.Task[None] | None = None
        self.last_frame = -1
        self.last_error: str | None = None
        self.session_epoch: str | None = None
        self.world_frame: str | None = None

    async def start(self) -> None:
        if self.task is None:
            self.stop_event.clear()
            self.task = asyncio.create_task(self._run(), name="alignment-point-cloud")

    async def stop(self) -> None:
        self.stop_event.set()
        if self.task:
            await self.task
            self.task = None

    async def clear(self) -> None:
        async with self.lock:
            self.chunks.clear()
            self.last_frame = -1

    async def _run(self) -> None:
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                await self._capture_once()
                self.last_error = None
            except Exception as error:
                self.last_error = str(error)
            try:
                await asyncio.wait_for(
                    self.stop_event.wait(),
                    timeout=max(0.02, self.period_s - (time.monotonic() - started)),
                )
            except TimeoutError:
                pass

    async def _capture_once(self) -> None:
        frame = await self.capture.capture(attempts=2)
        if frame.frame_number <= self.last_frame and frame.session_epoch == self.session_epoch:
            return
        if frame.session_epoch != self.session_epoch:
            async with self.lock:
                self.chunks.clear()
            self.session_epoch = frame.session_epoch
        transform = await self.fabric.transform(
            from_frame=frame.camera_frame,
            to_frame=frame.world_frame,
            at_us=frame.timestamp_us,
            max_extrapolation_us=750_000,
            session_epoch=frame.session_epoch,
        )
        points = self._make_points(frame.rgb, frame.depth_m, frame.intrinsics, transform)
        self.last_frame = frame.frame_number
        self.world_frame = frame.world_frame
        if points.size:
            async with self.lock:
                now = time.monotonic()
                self.chunks.append(PointChunk(now, points))
                self._purge(now)
                self._limit()

    def _make_points(
        self,
        rgb: np.ndarray,
        depth: np.ndarray,
        intrinsics: dict[str, Any],
        transform: dict[str, Any],
    ) -> np.ndarray:
        rows = np.arange(0, min(rgb.shape[0], depth.shape[0]), self.stride)
        columns = np.arange(0, min(rgb.shape[1], depth.shape[1]), self.stride)
        grid_x, grid_y = np.meshgrid(columns, rows)
        z = depth[grid_y, grid_x]
        valid = np.isfinite(z) & (z >= 0.2) & (z <= 8.0)
        if not np.any(valid):
            return np.empty((0, 6), np.float32)
        u, v, z = grid_x[valid], grid_y[valid], z[valid]
        fx, fy = float(intrinsics["fx"]), float(intrinsics["fy"])
        cx, cy = float(intrinsics["cx"]), float(intrinsics["cy"])
        camera = np.column_stack(((u - cx) * z / fx, (v - cy) * z / fy, z))
        rotation = quaternion_xyzw_to_matrix(transform["rotation_xyzw"])
        translation = np.asarray(transform["translation_m"], dtype=np.float64)
        world = (rotation @ camera.T).T + translation
        colors = rgb[grid_y[valid], grid_x[valid]].astype(np.float32) / 255.0
        return np.concatenate((world.astype(np.float32), colors), axis=1)

    def _purge(self, now: float) -> None:
        while self.chunks and now - self.chunks[0].created_monotonic > self.retention_s:
            self.chunks.popleft()

    def _limit(self) -> None:
        count = sum(chunk.points_xyzrgb.shape[0] for chunk in self.chunks)
        while len(self.chunks) > 1 and count > self.max_points:
            count -= self.chunks.popleft().points_xyzrgb.shape[0]
        if self.chunks and count > self.max_points:
            latest = self.chunks[-1]
            step = max(1, int(np.ceil(latest.points_xyzrgb.shape[0] / self.max_points)))
            latest.points_xyzrgb = latest.points_xyzrgb[::step][: self.max_points]

    async def snapshot_binary(self) -> bytes:
        async with self.lock:
            self._purge(time.monotonic())
            if self.chunks:
                records = np.concatenate(
                    [chunk.points_xyzrgb for chunk in self.chunks],
                    axis=0,
                ).astype("<f4", copy=False)
            else:
                records = np.empty((0, 6), dtype="<f4")
        return struct.pack("<I", records.shape[0]) + records.tobytes()

    async def status(self) -> dict[str, Any]:
        async with self.lock:
            count = sum(chunk.points_xyzrgb.shape[0] for chunk in self.chunks)
            return {
                "point_count": count,
                "world_frame": self.world_frame,
                "session_epoch": self.session_epoch,
                "last_frame": self.last_frame,
                "last_error": self.last_error,
                "running": self.task is not None and not self.task.done(),
            }
