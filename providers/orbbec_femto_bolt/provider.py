"""Resource Provider wrapper for the Orbbec Femto Bolt CameraHost.

CameraHost owns the device and writes high-volume payloads into Windows named
shared memory. This wrapper owns lifecycle and publishes small observations and
BufferRefs into the World State Fabric.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

LOCAL_PYTHON_ROOT = Path(__file__).resolve().parent / "python"
if str(LOCAL_PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(LOCAL_PYTHON_ROOT))

import httpx
import numpy as np

from orbbec_femto_provider.device_calibration import (
    AccelerometerCalibration,
    DeviceIdentityError,
    load_or_create_accelerometer_calibration,
)
from orbbec_femto_provider.data_routes import (
    DIRECT_RGBD_ROUTE_CAPABILITY,
    GENERIC_RGBD_ROUTE_CAPABILITY,
    build_direct_rgbd_route,
    build_generic_rgbd_route,
    build_rgbd_route_set,
)
from orbbec_femto_provider.shared_memory_access import (
    BufferRef,
    CameraSharedMemory,
    FRAME_METADATA_NAMES,
    ImuSample,
    STREAM_ACCEL,
    STREAM_ALIGNED_DEPTH,
    STREAM_CALIBRATION,
    STREAM_COLOR,
    STREAM_DEPTH,
    STREAM_GYRO,
    STREAM_IR,
    STREAM_POINT_CLOUD,
    STREAM_STATUS,
)

COLOR_FRAME = "femto_bolt_color_optical_frame"
DEPTH_FRAME = "femto_bolt_depth_optical_frame"
IR_FRAME = "femto_bolt_ir_optical_frame"
IMU_FRAME = "femto_bolt_imu_frame"
CAMERA_OPTICAL_CONVENTION_ID = (
    "CAMERA_OPTICAL_X_RIGHT_Y_DOWN_Z_FORWARD_V1"
)
CALIBRATION_REPUBLISH_INTERVAL_S = 2.0


class FemtoBoltProvider:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.provider_id = "camera.femto_bolt"
        self.instance_id = str(uuid.uuid4())
        self.boot_id = str(uuid.uuid4())
        self.shutdown_event = threading.Event()
        self.lock = threading.RLock()
        self.residency = "WARM"
        self.health = "HEALTHY"
        self.ready = False
        self.last_error: Optional[str] = None
        self.manager_error: Optional[str] = None
        self.route_publish_error: Optional[str] = None
        self.native_process: Optional[subprocess.Popen[str]] = None
        self.reader: Optional[CameraSharedMemory] = None
        self.http = httpx.Client(timeout=3.0)
        self.last_sequences: dict[str, int] = {}
        self.last_calibration: Optional[str] = None
        self.last_calibration_publish_monotonic: Optional[float] = None
        self.last_status: Optional[str] = None
        self.last_device_info_signature: Optional[str] = None
        self.last_accel_calibration_signature: Optional[str] = None
        self.last_transform_signature: Optional[str] = None
        self.accelerometer_calibration: Optional[AccelerometerCalibration] = None
        self.accelerometer_calibration_mtime_ns: Optional[int] = None
        self.device_identity_error: Optional[str] = None
        self.calibration_valid = False
        self.imu_calibration_valid = False
        self.ir_calibration_valid = False
        self.calibration_revision: Optional[str] = None
        self.stream_last_seen: dict[str, float] = {}
        self.started_hot_at: Optional[float] = None
        self.bundle_sequence = 0
        self.last_rgbd_bundle_key: Optional[tuple[int, int, int]] = None
        self.last_imu_bundle_key: Optional[tuple[int, int]] = None
        self.data_route_sequence = 0
        self.last_data_route_publish_monotonic = 0.0
        self.latest_refs: dict[str, BufferRef] = {}
        self.latest_rgbd_pair: Optional[tuple[BufferRef, BufferRef]] = None
        self.latest_rgbd_sync_details: dict[str, Any] = {}
        self.latest_imu: dict[str, ImuSample] = {}
        self.readiness_details = self._empty_readiness()

    def _empty_readiness(self) -> dict[str, bool]:
        return {
            "rgb": False,
            "depth": False,
            "ir": False,
            "aligned_depth": False,
            "point_cloud": False,
            "accel": False,
            "gyro": False,
            "calibration": False,
            "imu_calibration": False,
            "ir_geometry": False,
            "frame_metadata": False,
            "rgbd_sync": False,
            "device_info": False,
        }

    def register(self) -> None:
        response = self.http.post(
            f"{self.args.manager_url}/v1/providers/register",
            json=self._status_payload(),
        )
        response.raise_for_status()
        self._publish_rgbd_routes_best_effort(force=True)

    def start_hot(self) -> dict[str, Any]:
        with self.lock:
            deadline = time.monotonic() + self.args.camera_start_timeout
            if (
                self.native_process is not None
                and self.native_process.poll() is None
                and self.reader is not None
            ):
                self.residency = "HOT"
                self._refresh_readiness()
                self._heartbeat()
                return {
                    "status": "already_hot",
                    "native_pid": self.native_process.pid,
                    "ready": self.ready,
                }

            self.ready = False
            self.health = "HEALTHY"
            self.last_error = None
            self.manager_error = None
            self.readiness_details = self._empty_readiness()
            self.stream_last_seen.clear()
            self.calibration_valid = False
            self.imu_calibration_valid = False
            self.ir_calibration_valid = False
            self.calibration_revision = None
            self.last_sequences.clear()
            self.last_calibration = None
            self.last_calibration_publish_monotonic = None
            self.last_status = None
            self.last_device_info_signature = None
            self.last_accel_calibration_signature = None
            self.last_transform_signature = None
            self.accelerometer_calibration = None
            self.accelerometer_calibration_mtime_ns = None
            self.device_identity_error = None
            self.latest_refs.clear()
            self.latest_imu.clear()
            self.last_rgbd_bundle_key = None
            self.last_imu_bundle_key = None
            self.last_data_route_publish_monotonic = 0.0
            self._close_reader()
            self._stop_native(force=True)

            native_exe = Path(self.args.native_exe)
            if not native_exe.exists():
                raise FileNotFoundError(
                    f"CameraHost.exe not found at {native_exe}. "
                    "Run providers/orbbec_femto_bolt/scripts/setup.ps1 first."
                )

            command = [
                str(native_exe),
                "--mapping-name",
                self.args.mapping_name,
            ]
            if self.args.disable_ir:
                command.append("--no-ir")
            if self.args.disable_imu:
                command.append("--no-imu")
            if self.args.disable_frame_sync:
                command.append("--no-frame-sync")
            if self.args.disable_hardware_d2c:
                command.append("--no-hardware-d2c")
            if self.args.disable_aligned_depth:
                command.append("--no-aligned-depth")
            if self.args.point_cloud_mode == "off":
                command.append("--no-point-cloud")
            elif self.args.point_cloud_mode == "xyzrgb":
                command.append("--rgb-point-cloud-experimental")

            creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            self.native_process = subprocess.Popen(
                command,
                cwd=str(native_exe.parent),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                creationflags=creation_flags,
            )
            threading.Thread(target=self._pipe_native_logs, daemon=True).start()

            self.last_error = "waiting for CameraHost shared-memory mapping"
            self._heartbeat()
            heartbeat_at = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                if self.native_process.poll() is not None:
                    raise RuntimeError(
                        f"CameraHost exited with code {self.native_process.returncode}"
                    )
                try:
                    self.reader = CameraSharedMemory(self.args.mapping_name).open()
                    self._ensure_device_calibration()
                    break
                except Exception as error:
                    self.last_error = str(error)
                    now = time.monotonic()
                    if now >= heartbeat_at:
                        self._heartbeat()
                        heartbeat_at = now + 1.0
                    time.sleep(0.2)
            else:
                raise TimeoutError(
                    "CameraHost did not publish its shared-memory mapping before the deadline"
                )

            self.residency = "HOT"
            self.started_hot_at = time.monotonic()
            self.last_error = "waiting for RGB and depth frames"
            self._refresh_readiness()
            self._heartbeat()
            self._publish_rgbd_routes_best_effort(force=True)
            return {
                "status": "hot",
                "native_pid": self.native_process.pid,
                "ready": self.ready,
                "features": self._configured_features(),
            }

    def enter_warm(self) -> dict[str, Any]:
        with self.lock:
            self.ready = False
            self.residency = "WARM"
            self._close_reader()
            self._stop_native(force=False)
            self._publish_rgbd_routes_best_effort(force=True)
            return {"status": "warm"}

    def stop(self) -> dict[str, Any]:
        self.shutdown_event.set()
        with self.lock:
            self.ready = False
            self.residency = "STOPPING"
            self._close_reader()
            self._stop_native(force=False)
            self._publish_rgbd_routes_best_effort(force=True)
        return {"status": "stopping"}

    def run(self) -> int:
        self.register()
        self.start_hot()
        heartbeat_at = 0.0
        while not self.shutdown_event.is_set():
            now = time.monotonic()
            if now >= heartbeat_at:
                self._heartbeat()
                heartbeat_at = now + 1.0

            if self.native_process is not None and self.native_process.poll() is not None:
                code = self.native_process.returncode
                self.ready = False
                self.health = "UNHEALTHY"
                self.last_error = f"CameraHost exited unexpectedly with code {code}"
                self._heartbeat()
                raise RuntimeError(self.last_error)

            if self.residency == "HOT":
                try:
                    self._publish_latest()
                except Exception as error:
                    self.health = "DEGRADED"
                    self.last_error = str(error)
                    time.sleep(0.1)
            else:
                time.sleep(0.1)

        self._close_reader()
        self._stop_native(force=False)
        return 0

    def _calibration_root(self) -> Path:
        if self.args.calibration_root:
            return Path(self.args.calibration_root).resolve()
        if self.args.workspace_root:
            workspace_root = Path(self.args.workspace_root).resolve()
        else:
            workspace_root = Path(__file__).resolve().parents[2]
        return workspace_root / "config" / "calibration" / "devices"

    def _ensure_device_calibration(self, *, force: bool = False) -> None:
        reader = self.reader
        if reader is None:
            return
        header = reader.header or reader.refresh()
        if not header.device_serial:
            self.device_identity_error = "camera did not expose a persistent serial number"
            self.accelerometer_calibration = None
            return
        try:
            calibration = load_or_create_accelerometer_calibration(
                self._calibration_root(),
                manufacturer="Orbbec",
                model="Femto Bolt",
                serial_number=header.device_serial,
                firmware_version=header.firmware_version or None,
            )
        except DeviceIdentityError as error:
            self.device_identity_error = str(error)
            self.accelerometer_calibration = None
            return
        except Exception as error:
            self.device_identity_error = f"accelerometer calibration load failed: {error}"
            self.accelerometer_calibration = None
            return

        try:
            mtime_ns = calibration.path.stat().st_mtime_ns
        except OSError:
            mtime_ns = None
        if (
            not force
            and self.accelerometer_calibration is not None
            and self.accelerometer_calibration.path == calibration.path
            and self.accelerometer_calibration_mtime_ns == mtime_ns
        ):
            return
        self.accelerometer_calibration = calibration
        self.accelerometer_calibration_mtime_ns = mtime_ns
        self.device_identity_error = None
        self.last_accel_calibration_signature = None

    def reload_calibration(self) -> dict[str, Any]:
        with self.lock:
            self._ensure_device_calibration(force=True)
            calibration = self.accelerometer_calibration
            if calibration is None:
                raise RuntimeError(self.device_identity_error or "accelerometer calibration unavailable")
            return {
                "status": "reloaded",
                "calibration_status": calibration.status,
                "calibration_revision": calibration.revision,
                "path": str(calibration.path),
                "canonical_device_id": calibration.canonical_device_id,
            }

    def _correct_accelerometer(self, sample: ImuSample) -> dict[str, Any]:
        data = sample.to_dict()
        calibration = self.accelerometer_calibration
        raw = {"x": sample.x, "y": sample.y, "z": sample.z}
        if calibration is None:
            data.update(
                {
                    "uncalibrated_input": raw,
                    "calibration_status": "UNKNOWN_DEVICE_ID",
                    "calibration_revision": None,
                    "calibration_error": self.device_identity_error,
                }
            )
            return data
        corrected = calibration.apply(sample.x, sample.y, sample.z)
        data.update(
            {
                "x": corrected[0],
                "y": corrected[1],
                "z": corrected[2],
                "uncalibrated_input": raw,
                "calibration_status": calibration.status,
                "calibration_revision": calibration.revision,
                "correction_equation": "corrected_equals_scale_times_input_plus_offset",
                "scale": list(calibration.scale),
                "offset": list(calibration.offset),
            }
        )
        return data

    @staticmethod
    def _matrix_to_quaternion_xyzw(values: Any) -> list[float]:
        if not isinstance(values, list) or len(values) != 9:
            raise ValueError("rotation matrix must have 9 values")
        m = [float(value) for value in values]
        trace = m[0] + m[4] + m[8]
        if trace > 0.0:
            s = math.sqrt(trace + 1.0) * 2.0
            w = 0.25 * s
            x = (m[7] - m[5]) / s
            y = (m[2] - m[6]) / s
            z = (m[3] - m[1]) / s
        elif m[0] > m[4] and m[0] > m[8]:
            s = math.sqrt(1.0 + m[0] - m[4] - m[8]) * 2.0
            w = (m[7] - m[5]) / s
            x = 0.25 * s
            y = (m[1] + m[3]) / s
            z = (m[2] + m[6]) / s
        elif m[4] > m[8]:
            s = math.sqrt(1.0 + m[4] - m[0] - m[8]) * 2.0
            w = (m[2] - m[6]) / s
            x = (m[1] + m[3]) / s
            y = 0.25 * s
            z = (m[5] + m[7]) / s
        else:
            s = math.sqrt(1.0 + m[8] - m[0] - m[4]) * 2.0
            w = (m[3] - m[1]) / s
            x = (m[2] + m[6]) / s
            y = (m[5] + m[7]) / s
            z = 0.25 * s
        norm = math.sqrt(x * x + y * y + z * z + w * w)
        if norm <= 1e-12:
            return [0.0, 0.0, 0.0, 1.0]
        return [x / norm, y / norm, z / norm, w / norm]

    def _static_transform_observations(
        self,
        calibration_data: dict[str, Any],
        *,
        observed_at_us: int,
    ) -> list[dict[str, Any]]:
        transforms: list[tuple[str, str, str, dict[str, Any]]] = []
        depth_to_color = calibration_data.get("depth_to_color")
        if isinstance(depth_to_color, dict):
            transforms.append(
                (
                    "transform.camera.color_from_depth",
                    COLOR_FRAME,
                    DEPTH_FRAME,
                    depth_to_color,
                )
            )
        imu = calibration_data.get("imu") or {}
        accelerometer_to_color = imu.get("accelerometer_to_color")
        if isinstance(accelerometer_to_color, dict):
            transforms.append(
                (
                    "transform.camera.color_from_imu",
                    COLOR_FRAME,
                    IMU_FRAME,
                    accelerometer_to_color,
                )
            )

        observations: list[dict[str, Any]] = []
        for stream, parent_frame, child_frame, extrinsic in transforms:
            rotation = self._matrix_to_quaternion_xyzw(extrinsic.get("rot"))
            translation_mm = extrinsic.get("trans")
            if not isinstance(translation_mm, list) or len(translation_mm) != 3:
                continue
            translation_m = [float(value) / 1000.0 for value in translation_mm]
            observations.append(
                self._observation(
                    stream=stream,
                    schema="physical_agent.transform",
                    sequence=observed_at_us,
                    observed_at_us=observed_at_us,
                    coordinate_frame=parent_frame,
                    calibration_revision=self.calibration_revision,
                    data={
                        "parent_frame": parent_frame,
                        "child_frame": child_frame,
                        "translation_m": translation_m,
                        "rotation_xyzw": rotation,
                        "is_static": True,
                        "authority": f"{self.provider_id}:factory_calibration",
                        "session_epoch": None,
                        "continuity": "STATIC",
                    },
                    freshness_ms=None,
                )
            )
        return observations

    def _configured_features(self) -> dict[str, Any]:
        return {
            "rgb": True,
            "depth": True,
            "ir": not self.args.disable_ir,
            "imu": not self.args.disable_imu,
            "frame_sync": not self.args.disable_frame_sync,
            "hardware_d2c": not self.args.disable_hardware_d2c,
            "aligned_depth": not self.args.disable_aligned_depth,
            "point_cloud_mode": self.args.point_cloud_mode,
            "per_frame_metadata": True,
            "global_timestamp_requested": True,
        }

    def _refresh_readiness(self) -> dict[str, bool]:
        status = self._inspect_streams()
        self.ready = status["rgb"] and status["depth"]

        missing_required = [name for name in ("rgb", "depth") if not status[name]]
        optional_expected = {
            "ir": not self.args.disable_ir,
            "aligned_depth": not self.args.disable_aligned_depth,
            "point_cloud": self.args.point_cloud_mode != "off",
            "accel": not self.args.disable_imu,
            "gyro": not self.args.disable_imu,
            "calibration": True,
            "imu_calibration": not self.args.disable_imu,
            "ir_geometry": not self.args.disable_ir,
            "rgbd_sync": not self.args.disable_frame_sync,
            "device_info": True,
        }
        missing_optional = [
            name for name, expected in optional_expected.items() if expected and not status[name]
        ]
        startup_age = (
            time.monotonic() - self.started_hot_at if self.started_hot_at is not None else 0.0
        )

        if missing_required:
            self.health = "DEGRADED"
            self.last_error = f"waiting for required camera streams: {', '.join(missing_required)}"
        elif missing_optional and startup_age >= self.args.optional_feature_grace:
            self.health = "DEGRADED"
            self.last_error = f"optional camera capabilities unavailable: {', '.join(missing_optional)}"
        else:
            self.health = "HEALTHY"
            self.last_error = None
        return status

    def _safe_read_imu(self, stream_kind: int) -> Optional[ImuSample]:
        reader = self.reader
        if reader is None:
            return None
        try:
            return reader.read_imu(stream_kind)
        except RuntimeError:
            # High-rate ring slots can be recycled between metadata and payload reads.
            # The next provider iteration will acquire a fresh slot.
            return None

    def _safe_read_text(self, stream_kind: int) -> Optional[str]:
        reader = self.reader
        if reader is None:
            return None
        try:
            return reader.read_text(stream_kind)
        except RuntimeError:
            return None

    def _inspect_streams(self) -> dict[str, bool]:
        reader = self.reader
        if reader is None:
            self.readiness_details = self._empty_readiness()
            return dict(self.readiness_details)

        self._ensure_device_calibration()
        now = time.monotonic()
        checks = (
            ("rgb", STREAM_COLOR),
            ("depth", STREAM_DEPTH),
            ("ir", STREAM_IR),
            ("aligned_depth", STREAM_ALIGNED_DEPTH),
            ("point_cloud", STREAM_POINT_CLOUD),
        )
        for name, kind in checks:
            reference = reader.latest_ref(kind)
            if reference is not None and reference.payload_bytes > 0:
                self.stream_last_seen[name] = now
                self.latest_refs[name] = reference
                if reference.frame_metadata:
                    self.stream_last_seen["frame_metadata"] = now

        for name, kind in (("accel", STREAM_ACCEL), ("gyro", STREAM_GYRO)):
            sample = self._safe_read_imu(kind)
            if sample is not None:
                self.stream_last_seen[name] = now
                self.latest_imu[name] = sample

        if reader.header is not None and reader.header.device_name:
            self.stream_last_seen["device_info"] = now

        calibration = self._safe_read_text(STREAM_CALIBRATION)
        if calibration is not None:
            (
                self.calibration_valid,
                self.ir_calibration_valid,
                self.imu_calibration_valid,
            ) = self._calibration_readiness(calibration)
            if self.calibration_valid:
                self.stream_last_seen["calibration"] = now
                self.calibration_revision = hashlib.sha256(
                    calibration.encode("utf-8")
                ).hexdigest()[:16]
            if self.imu_calibration_valid:
                self.stream_last_seen["imu_calibration"] = now
            if self.ir_calibration_valid:
                self.stream_last_seen["ir_geometry"] = now

        rgb_refs = reader.recent_refs(STREAM_COLOR)
        depth_refs = reader.recent_refs(STREAM_DEPTH)
        self.latest_rgbd_pair = self._newest_synchronized_pair(
            rgb_refs,
            depth_refs,
            maximum_delta_us=self.args.rgbd_max_delta_us,
        )
        self.latest_rgbd_sync_details = self._rgbd_sync_details(
            rgb_refs,
            depth_refs,
            pair=self.latest_rgbd_pair,
            maximum_delta_us=self.args.rgbd_max_delta_us,
        )
        if self.latest_rgbd_pair is not None:
            self.stream_last_seen["rgbd_sync"] = now

        self.readiness_details = {
            name: now - self.stream_last_seen.get(name, float("-inf")) <= 2.0
            for name in self._empty_readiness()
        }
        self.readiness_details["calibration"] = self.calibration_valid
        self.readiness_details["imu_calibration"] = self.imu_calibration_valid
        self.readiness_details["ir_geometry"] = self.ir_calibration_valid
        return dict(self.readiness_details)

    @staticmethod
    def _calibration_readiness(raw: Optional[str]) -> tuple[bool, bool, bool]:
        if not raw:
            return False, False, False
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return False, False, False

        rgb = data.get("rgb_intrinsic") or {}
        depth = data.get("depth_intrinsic") or {}
        rgbd_valid = all(
            float(values.get(field, 0) or 0) > 0
            for values in (rgb, depth)
            for field in ("fx", "fy", "width", "height")
        )

        infrared = data.get("infrared") or {}
        ir_intrinsic = infrared.get("intrinsic") or {}
        ir_valid = all(
            float(ir_intrinsic.get(field, 0) or 0) > 0
            for field in ("fx", "fy", "width", "height")
        )

        imu = data.get("imu") or {}
        accel = imu.get("accelerometer") or {}
        gyro = imu.get("gyroscope") or {}
        imu_valid = bool(accel.get("intrinsic")) and bool(gyro.get("intrinsic"))
        return rgbd_valid, ir_valid, imu_valid

    @staticmethod
    def _ref_timestamp(reference: BufferRef) -> int:
        return int(
            reference.global_timestamp_us
            or reference.system_timestamp_us
            or reference.device_timestamp_us
        )

    @staticmethod
    def _sample_timestamp(sample: ImuSample) -> int:
        return int(
            sample.global_timestamp_us
            or sample.system_timestamp_us
            or sample.device_timestamp_us
        )

    @classmethod
    def _newest_synchronized_pair(
        cls,
        first_refs: list[BufferRef],
        second_refs: list[BufferRef],
        *,
        maximum_delta_us: int,
    ) -> Optional[tuple[BufferRef, BufferRef]]:
        candidates: list[
            tuple[tuple[int, int], BufferRef, BufferRef]
        ] = []
        for first in first_refs:
            first_timestamp_us = cls._ref_timestamp(first)
            if first_timestamp_us <= 0:
                continue
            for second in second_refs:
                second_timestamp_us = cls._ref_timestamp(second)
                if second_timestamp_us <= 0:
                    continue
                delta_us = abs(first_timestamp_us - second_timestamp_us)
                if maximum_delta_us > 0 and delta_us > maximum_delta_us:
                    continue
                candidates.append(
                    (
                        (min(first_timestamp_us, second_timestamp_us), -delta_us),
                        first,
                        second,
                    )
                )
        if not candidates:
            return None
        _, first, second = max(candidates, key=lambda item: item[0])
        return first, second

    @classmethod
    def _nearest_synchronized_ref(
        cls,
        target: BufferRef,
        candidates: list[BufferRef],
        *,
        maximum_delta_us: int,
    ) -> Optional[BufferRef]:
        target_timestamp_us = cls._ref_timestamp(target)
        matched: list[tuple[tuple[int, int, int], BufferRef]] = []
        for candidate in candidates:
            candidate_timestamp_us = cls._ref_timestamp(candidate)
            delta_us = abs(candidate_timestamp_us - target_timestamp_us)
            if candidate_timestamp_us <= 0:
                continue
            if maximum_delta_us > 0 and delta_us > maximum_delta_us:
                continue
            exact_frame = int(candidate.frame_number == target.frame_number)
            matched.append(
                (
                    (exact_frame, -delta_us, candidate_timestamp_us),
                    candidate,
                )
            )
        if not matched:
            return None
        return max(matched, key=lambda item: item[0])[1]

    @classmethod
    def _rgbd_sync_details(
        cls,
        rgb_refs: list[BufferRef],
        depth_refs: list[BufferRef],
        *,
        pair: Optional[tuple[BufferRef, BufferRef]],
        maximum_delta_us: int,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "maximum_delta_us": int(maximum_delta_us),
            "retained_rgb_frames": len(rgb_refs),
            "retained_depth_frames": len(depth_refs),
            "synchronized": pair is not None,
            "timestamp_selection": "global_then_system_then_device",
        }
        if pair is not None:
            rgb, depth = pair
            result.update(
                {
                    "rgb_frame_number": int(rgb.frame_number),
                    "depth_frame_number": int(depth.frame_number),
                    "timestamp_delta_us": int(
                        cls._ref_timestamp(depth) - cls._ref_timestamp(rgb)
                    ),
                }
            )
        elif rgb_refs and depth_refs:
            rgb = rgb_refs[-1]
            depth = depth_refs[-1]
            result.update(
                {
                    "newest_rgb_frame_number": int(rgb.frame_number),
                    "newest_depth_frame_number": int(depth.frame_number),
                    "newest_timestamp_delta_us": int(
                        cls._ref_timestamp(depth) - cls._ref_timestamp(rgb)
                    ),
                }
            )
        return result

    def _publish_latest(self) -> None:
        reader = self.reader
        if reader is None:
            time.sleep(0.05)
            return

        self._refresh_readiness()
        self._publish_rgbd_routes_best_effort()
        observations: list[dict[str, Any]] = []
        calibration_publication: Optional[tuple[str, float]] = None

        stream_specs = [
            (STREAM_COLOR, "rgb", "camera.rgb.frame_ref", COLOR_FRAME),
            (STREAM_DEPTH, "depth", "camera.depth.frame_ref", DEPTH_FRAME),
            (STREAM_IR, "ir", "camera.ir.frame_ref", IR_FRAME),
            (
                STREAM_ALIGNED_DEPTH,
                "aligned_depth",
                "camera.depth_aligned_to_rgb.frame_ref",
                COLOR_FRAME,
            ),
        ]
        point_stream = (
            "camera.point_cloud.xyzrgb.frame_ref"
            if self.args.point_cloud_mode == "xyzrgb"
            else "camera.point_cloud.xyz.frame_ref"
        )
        stream_specs.append(
            (
                STREAM_POINT_CLOUD,
                "point_cloud",
                point_stream,
                COLOR_FRAME if self.args.point_cloud_mode == "xyzrgb" else DEPTH_FRAME,
            )
        )

        current_refs: dict[str, BufferRef] = {}
        for stream_kind, short_name, stream_name, coordinate_frame in stream_specs:
            reference = reader.latest_ref(stream_kind)
            if reference is None:
                continue
            current_refs[short_name] = reference
            self.latest_refs[short_name] = reference
            if self.last_sequences.get(stream_name) == reference.generation:
                continue
            self.last_sequences[stream_name] = reference.generation
            observations.append(
                self._observation(
                    stream=stream_name,
                    schema="physical_agent.buffer_ref",
                    sequence=reference.frame_number,
                    observed_at_us=self._ref_timestamp(reference),
                    frame_id=f"{reference.stream_name}:{reference.frame_number}",
                    coordinate_frame=coordinate_frame,
                    calibration_revision=self.calibration_revision,
                    data=reference.to_dict(),
                    freshness_ms=1000,
                )
            )

        for stream_kind, short_name, stream_name, units in (
            (STREAM_ACCEL, "accel", "camera.imu.accel", "m/s^2"),
            (STREAM_GYRO, "gyro", "camera.imu.gyro", "rad/s"),
        ):
            sample = self._safe_read_imu(stream_kind)
            if sample is None:
                continue
            self.latest_imu[short_name] = sample
            if self.last_sequences.get(stream_name) == sample.frame_number:
                continue
            self.last_sequences[stream_name] = sample.frame_number
            if short_name == "accel":
                data = self._correct_accelerometer(sample)
                effective_revision = (
                    self.accelerometer_calibration.revision
                    if self.accelerometer_calibration is not None
                    else None
                )
            else:
                data = sample.to_dict()
                data.update(
                    {
                        "calibration_status": "FACTORY_ONLY",
                        "calibration_revision": self.calibration_revision,
                    }
                )
                effective_revision = self.calibration_revision
            data.update(
                {
                    "units": units,
                    "coordinate_frame": IMU_FRAME,
                    "temperature_units": "degC",
                }
            )
            observations.append(
                self._observation(
                    stream=stream_name,
                    schema="physical_agent.imu_sample",
                    sequence=sample.frame_number,
                    observed_at_us=self._sample_timestamp(sample),
                    coordinate_frame=IMU_FRAME,
                    calibration_revision=effective_revision,
                    data=data,
                    freshness_ms=500,
                )
            )

        calibration = self._safe_read_text(STREAM_CALIBRATION)
        calibration_publish_monotonic = time.monotonic()
        if self._calibration_publish_due(
            calibration,
            now_monotonic=calibration_publish_monotonic,
        ):
            try:
                calibration_data: Any = json.loads(calibration)
            except json.JSONDecodeError:
                calibration_data = {"raw": calibration}
            calibration_data["coordinate_frames"] = {
                "color": COLOR_FRAME,
                "depth": DEPTH_FRAME,
                "infrared": IR_FRAME,
                "imu": IMU_FRAME,
            }
            calibration_data["coordinate_conventions"] = {
                "color": CAMERA_OPTICAL_CONVENTION_ID,
                "depth": CAMERA_OPTICAL_CONVENTION_ID,
                "infrared": CAMERA_OPTICAL_CONVENTION_ID,
                "imu": "HARDWARE_CALIBRATED_LOCAL_FRAME",
            }
            calibration_data["coordinate_axis_names"] = {
                "color": [
                    "camera_system_x",
                    "camera_system_y",
                    "camera_system_z",
                ],
                "depth": [
                    "camera_system_x",
                    "camera_system_y",
                    "camera_system_z",
                ],
                "infrared": [
                    "camera_system_x",
                    "camera_system_y",
                    "camera_system_z",
                ],
            }
            calibration_data["extrinsic_semantics"] = {
                "source_frame": DEPTH_FRAME,
                "target_frame": COLOR_FRAME,
                "translation_units": "millimeters",
            }
            calibration_data["revision"] = self.calibration_revision
            now_us = int(time.time() * 1_000_000)
            observations.append(
                self._observation(
                    stream="camera.calibration",
                    schema="physical_agent.camera_calibration",
                    sequence=now_us,
                    observed_at_us=now_us,
                    calibration_revision=self.calibration_revision,
                    data=calibration_data,
                    freshness_ms=None,
                )
            )
            observations.extend(
                self._static_transform_observations(
                    calibration_data,
                    observed_at_us=now_us,
                )
            )
            calibration_publication = (
                calibration,
                calibration_publish_monotonic,
            )

        accel_calibration = self.accelerometer_calibration
        if accel_calibration is not None:
            calibration_signature = json.dumps(
                accel_calibration.document,
                sort_keys=True,
            )
            if calibration_signature != self.last_accel_calibration_signature:
                self.last_accel_calibration_signature = calibration_signature
                now_us = int(time.time() * 1_000_000)
                calibration_document = dict(accel_calibration.document)
                calibration_document["revision"] = accel_calibration.revision
                calibration_document["path"] = str(accel_calibration.path)
                observations.append(
                    self._observation(
                        stream="camera.imu.accel.calibration",
                        schema="physical_agent.imu_accelerometer_calibration",
                        sequence=now_us,
                        observed_at_us=now_us,
                        coordinate_frame=IMU_FRAME,
                        calibration_revision=accel_calibration.revision,
                        data=calibration_document,
                        freshness_ms=None,
                    )
                )

        device_info = self._device_info()
        signature = json.dumps(device_info, sort_keys=True)
        if signature != self.last_device_info_signature:
            self.last_device_info_signature = signature
            now_us = int(time.time() * 1_000_000)
            observations.append(
                self._observation(
                    stream="camera.device_info",
                    schema="physical_agent.camera_device_info",
                    sequence=now_us,
                    observed_at_us=now_us,
                    data=device_info,
                    freshness_ms=None,
                )
            )

        status = self._safe_read_text(STREAM_STATUS)
        if status and status != self.last_status:
            self.last_status = status
            now_us = int(time.time() * 1_000_000)
            observations.append(
                self._observation(
                    stream="camera.status",
                    schema="physical_agent.provider_status",
                    sequence=now_us,
                    observed_at_us=now_us,
                    data={"text": status},
                    freshness_ms=5000,
                )
            )

        pair = self.latest_rgbd_pair
        if pair is not None:
            rgb, depth = pair
            aligned_depth = self._nearest_synchronized_ref(
                rgb,
                reader.recent_refs(STREAM_ALIGNED_DEPTH),
                maximum_delta_us=self.args.rgbd_max_delta_us,
            )
            bundle = self._rgbd_bundle(rgb, depth, aligned_depth)
            bundle_key = (
                rgb.generation,
                depth.generation,
                aligned_depth.generation if aligned_depth is not None else 0,
            )
            if self.last_rgbd_bundle_key != bundle_key:
                self.last_rgbd_bundle_key = bundle_key
                self.bundle_sequence += 1
                observations.append(
                    self._observation(
                        stream="camera.rgbd.bundle",
                        schema="physical_agent.synchronized_buffer_bundle",
                        sequence=self.bundle_sequence,
                        observed_at_us=max(self._ref_timestamp(rgb), self._ref_timestamp(depth)),
                        coordinate_frame=COLOR_FRAME,
                        calibration_revision=self.calibration_revision,
                        data=bundle,
                        freshness_ms=1000,
                    )
                )

        accel = self.latest_imu.get("accel")
        gyro = self.latest_imu.get("gyro")
        if accel is not None and gyro is not None:
            imu_key = (accel.frame_number, gyro.frame_number)
            if self.last_imu_bundle_key != imu_key:
                self.last_imu_bundle_key = imu_key
                self.bundle_sequence += 1
                accel_ts = self._sample_timestamp(accel)
                gyro_ts = self._sample_timestamp(gyro)
                observations.append(
                    self._observation(
                        stream="camera.imu.bundle",
                        schema="physical_agent.synchronized_imu_bundle",
                        sequence=self.bundle_sequence,
                        observed_at_us=max(accel_ts, gyro_ts),
                        coordinate_frame=IMU_FRAME,
                        data={
                            "accelerometer": self._correct_accelerometer(accel),
                            "gyroscope": gyro.to_dict(),
                            "timestamp_delta_us": gyro_ts - accel_ts,
                            "units": {"accelerometer": "m/s^2", "gyroscope": "rad/s"},
                            "accelerometer_calibration_revision": (
                                self.accelerometer_calibration.revision
                                if self.accelerometer_calibration is not None
                                else None
                            ),
                            "sensor_calibration_revision": self.calibration_revision,
                        },
                        freshness_ms=500,
                    )
                )

        self._publish_observation_batch(
            observations,
            calibration_publication=calibration_publication,
        )
        time.sleep(self.args.poll_interval)

    def _calibration_publish_due(
        self,
        calibration: Optional[str],
        *,
        now_monotonic: float,
    ) -> bool:
        if not calibration:
            return False
        if calibration != self.last_calibration:
            return True
        published_at = self.last_calibration_publish_monotonic
        return (
            published_at is None
            or now_monotonic - published_at
            >= CALIBRATION_REPUBLISH_INTERVAL_S
        )

    def _publish_observation_batch(
        self,
        observations: list[dict[str, Any]],
        *,
        calibration_publication: Optional[tuple[str, float]] = None,
    ) -> None:
        if not observations:
            return
        response = self.http.post(
            f"{self.args.fabric_url}/v1/observations/batch",
            json={"observations": observations},
        )
        response.raise_for_status()
        if calibration_publication is not None:
            (
                self.last_calibration,
                self.last_calibration_publish_monotonic,
            ) = calibration_publication

    def _rgbd_bundle(
        self,
        rgb: BufferRef,
        depth: BufferRef,
        aligned_depth: Optional[BufferRef],
    ) -> dict[str, Any]:
        rgb_ts = self._ref_timestamp(rgb)
        depth_ts = self._ref_timestamp(depth)
        delta = depth_ts - rgb_ts
        result: dict[str, Any] = {
            "rgb": rgb.to_dict(),
            "depth": depth.to_dict(),
            "timestamp_delta_us": delta,
            "max_delta_us": self.args.rgbd_max_delta_us,
            "synchronized": abs(delta) <= self.args.rgbd_max_delta_us,
            "frame_sync_requested": not self.args.disable_frame_sync,
            "calibration_revision": self.calibration_revision,
            "coordinate_frames": {
                "rgb": COLOR_FRAME,
                "depth": DEPTH_FRAME,
                "aligned_depth": COLOR_FRAME,
            },
            "coordinate_conventions": {
                "rgb": CAMERA_OPTICAL_CONVENTION_ID,
                "depth": CAMERA_OPTICAL_CONVENTION_ID,
                "aligned_depth": CAMERA_OPTICAL_CONVENTION_ID,
            },
            "coordinate_axis_names": {
                "rgb": [
                    "camera_system_x",
                    "camera_system_y",
                    "camera_system_z",
                ],
                "depth": [
                    "camera_system_x",
                    "camera_system_y",
                    "camera_system_z",
                ],
                "aligned_depth": [
                    "camera_system_x",
                    "camera_system_y",
                    "camera_system_z",
                ],
            },
        }
        if aligned_depth is not None:
            result["depth_aligned_to_rgb"] = aligned_depth.to_dict()
        return result

    def _device_info(self) -> dict[str, Any]:
        header = None
        if self.reader is not None:
            try:
                header = self.reader.refresh()
            except RuntimeError:
                header = self.reader.header
        return {
            "provider_version": "0.4.1",
            "device_name": header.device_name if header else None,
            "serial_number": header.device_serial if header else None,
            "sdk_version": header.sdk_version if header else None,
            "firmware_version": header.firmware_version if header else None,
            "connection_type": header.connection_type if header else None,
            "device_uid": header.device_uid if header else None,
            "canonical_device_id": (
                self.accelerometer_calibration.canonical_device_id
                if self.accelerometer_calibration is not None
                else None
            ),
            "device_identity_error": self.device_identity_error,
            "accelerometer_calibration": (
                {
                    "status": self.accelerometer_calibration.status,
                    "revision": self.accelerometer_calibration.revision,
                    "path": str(self.accelerometer_calibration.path),
                }
                if self.accelerometer_calibration is not None
                else None
            ),
            "usb_pid": header.usb_pid if header else None,
            "usb_vid": header.usb_vid if header else None,
            "global_timestamp_supported": (
                header.global_timestamp_supported if header else False
            ),
            "global_timestamp_enabled": (
                header.global_timestamp_enabled if header else False
            ),
            "native_process_id": header.process_id if header else None,
            "shared_memory_layout_version": header.layout_version if header else None,
            "mapping_name": self.args.mapping_name,
            "mapping_bytes": header.total_bytes if header else None,
            "slot_header_bytes": header.slot_header_bytes if header else None,
            "frame_metadata_names": list(FRAME_METADATA_NAMES),
            "windows_frame_metadata_registration_may_be_required": True,
            "configured_features": self._configured_features(),
            "streams": [
                {
                    "name": stream.name,
                    "stream_kind": stream.stream_kind,
                    "payload_kind": stream.payload_kind,
                    "slot_count": stream.slot_count,
                    "slot_stride_bytes": stream.slot_stride_bytes,
                    "slot_payload_capacity_bytes": stream.slot_payload_capacity_bytes,
                    "base_offset_bytes": stream.base_offset_bytes,
                    "dropped_frame_count": stream.dropped_frame_count,
                }
                for stream in header.streams
            ] if header else [],
            "coordinate_frames": {
                "color": COLOR_FRAME,
                "depth": DEPTH_FRAME,
                "infrared": IR_FRAME,
                "imu": IMU_FRAME,
            },
            "coordinate_conventions": {
                "color": CAMERA_OPTICAL_CONVENTION_ID,
                "depth": CAMERA_OPTICAL_CONVENTION_ID,
                "infrared": CAMERA_OPTICAL_CONVENTION_ID,
                "imu": "HARDWARE_CALIBRATED_LOCAL_FRAME",
            },
            "coordinate_axis_names": {
                "optical": [
                    "camera_system_x",
                    "camera_system_y",
                    "camera_system_z",
                ],
            },
        }

    def _observation(
        self,
        *,
        stream: str,
        schema: str,
        sequence: int,
        observed_at_us: int,
        data: Any,
        freshness_ms: Optional[int],
        frame_id: Optional[str] = None,
        coordinate_frame: Optional[str] = None,
        calibration_revision: Optional[str] = None,
        clock_domain: Optional[str] = None,
        related_skill_id: Optional[str] = None,
    ) -> dict[str, Any]:
        coordinate_convention_id = (
            CAMERA_OPTICAL_CONVENTION_ID
            if coordinate_frame in {COLOR_FRAME, DEPTH_FRAME, IR_FRAME}
            else (
                "HARDWARE_CALIBRATED_LOCAL_FRAME"
                if coordinate_frame == IMU_FRAME
                else None
            )
        )
        coordinate_axis_names = (
            {
                "x": "camera_system_x",
                "y": "camera_system_y",
                "z": "camera_system_z",
            }
            if coordinate_convention_id
            == CAMERA_OPTICAL_CONVENTION_ID
            else None
        )
        return {
            "schema": schema,
            "schema_version": 1,
            "stream": stream,
            "provider_id": self.provider_id,
            "provider_instance_id": self.instance_id,
            "boot_id": self.boot_id,
            "sequence": max(0, int(sequence)),
            "observed_at_us": max(0, int(observed_at_us)),
            "freshness_ms": freshness_ms,
            "frame_id": frame_id,
            "coordinate_frame": coordinate_frame,
            "coordinate_convention_id": coordinate_convention_id,
            "coordinate_axis_names": coordinate_axis_names,
            "calibration_revision": calibration_revision,
            "clock_domain": clock_domain,
            "related_skill_id": related_skill_id,
            "valid": True,
            "data": data,
        }

    def _direct_rgbd_route(self) -> dict[str, Any]:
        readiness = self.readiness_details
        return build_direct_rgbd_route(
            provider_id=self.provider_id,
            provider_instance_id=self.instance_id,
            boot_id=self.boot_id,
            mapping_name=self.args.mapping_name,
            calibration_revision=self.calibration_revision,
            rgb_ready=bool(readiness["rgb"] and self.residency == "HOT"),
            depth_ready=bool(readiness["depth"] and self.residency == "HOT"),
        )

    def _generic_rgbd_route(self) -> dict[str, Any]:
        def reference(name: str) -> dict[str, Any] | None:
            current = self.latest_refs.get(name)
            return current.to_dict() if current is not None else None

        aligned_depth = self.latest_refs.get("aligned_depth")
        custom_alignment = {
            "implementation": "ORBBEC_ALIGN_FILTER",
            "processing_location": "CAMERA_HOST_BEFORE_SHARED_MEMORY_PUBLISH",
            "provider_writes_registered_product": True,
            "note": aligned_depth.note if aligned_depth is not None else None,
        }
        if aligned_depth is not None:
            custom_alignment.update(
                self._aligned_depth_validity_metadata(aligned_depth)
            )
        return build_generic_rgbd_route(
            provider_id=self.provider_id,
            provider_instance_id=self.instance_id,
            boot_id=self.boot_id,
            mapping_name=self.args.mapping_name,
            calibration_revision=self.calibration_revision,
            rgb_reference=reference("rgb"),
            depth_reference=reference("depth"),
            ir_reference=reference("ir"),
            aligned_depth_reference=reference("aligned_depth"),
            custom_alignment=custom_alignment,
        )

    def _aligned_depth_validity_metadata(
        self,
        reference: BufferRef,
    ) -> dict[str, Any]:
        if self.reader is None:
            return {
                "validity_status": "SHARED_MEMORY_READER_UNAVAILABLE",
            }
        try:
            payload = self.reader.read_ref(reference)
            width = int(reference.width)
            height = int(reference.height)
            stride_bytes = int(reference.stride_bytes)
            if min(width, height, stride_bytes) <= 0 or stride_bytes % 2 != 0:
                raise RuntimeError("aligned-depth grid or stride is invalid")
            required_bytes = height * stride_bytes
            if len(payload) < required_bytes:
                raise RuntimeError(
                    "aligned-depth payload is shorter than its declared stride"
                )
            stride_values = stride_bytes // 2
            values = np.frombuffer(
                payload,
                dtype="<u2",
                count=height * stride_values,
            ).reshape(height, stride_values)[:, :width]
            valid = values > 0
            valid_count = int(np.count_nonzero(valid))
            if valid_count == 0:
                return {
                    "validity_status": "NO_VALID_DEPTH",
                    "valid_fraction": 0.0,
                    "source_generation": int(reference.generation),
                    "source_frame_number": int(reference.frame_number),
                }
            rows, columns = np.nonzero(valid)
            x0 = int(columns.min())
            x1 = int(columns.max()) + 1
            y0 = int(rows.min())
            y1 = int(rows.max()) + 1
            return {
                "validity_status": "OBSERVED",
                "valid_boundary": {
                    "x": x0,
                    "y": y0,
                    "width": x1 - x0,
                    "height": y1 - y0,
                },
                "valid_fraction": float(valid_count / (width * height)),
                "boundary_method": "NONZERO_ALIGNED_DEPTH_AXIS_ALIGNED_BOUNDS",
                "source_generation": int(reference.generation),
                "source_frame_number": int(reference.frame_number),
                "source_global_timestamp_us": int(
                    reference.global_timestamp_us
                ),
                "source_system_timestamp_us": int(
                    reference.system_timestamp_us
                ),
                "source_device_timestamp_us": int(
                    reference.device_timestamp_us
                ),
            }
        except Exception as error:
            return {
                "validity_status": "UNAVAILABLE",
                "validity_error": str(error),
                "source_generation": int(reference.generation),
                "source_frame_number": int(reference.frame_number),
            }

    def _rgbd_routes(self) -> list[dict[str, Any]]:
        return [self._direct_rgbd_route(), self._generic_rgbd_route()]

    def _publish_rgbd_routes_best_effort(self, *, force: bool = False) -> None:
        now_monotonic = time.monotonic()
        if (
            not force
            and now_monotonic - self.last_data_route_publish_monotonic < 5.0
        ):
            return
        self.last_data_route_publish_monotonic = now_monotonic
        now_us = int(time.time() * 1_000_000)
        routes = self._rgbd_routes()
        route_set = build_rgbd_route_set(routes[0], routes[1])
        observation = self._observation(
            stream="camera.rgbd.data_routes",
            schema="physical_agent.data_route_set",
            sequence=self.data_route_sequence + 1,
            observed_at_us=now_us,
            calibration_revision=self.calibration_revision,
            data=route_set,
            freshness_ms=15_000,
        )
        try:
            response = self.http.post(
                f"{self.args.fabric_url}/v1/observations",
                json=observation,
            )
            response.raise_for_status()
        except Exception as error:
            self.route_publish_error = f"RGB-D data-route publish failed: {error}"
            return
        self.data_route_sequence += 1
        self.route_publish_error = None

    def _heartbeat(self) -> None:
        try:
            response = self.http.post(
                f"{self.args.manager_url}/v1/providers/heartbeat",
                json=self._status_payload(),
            )
            response.raise_for_status()
            self.manager_error = None
        except Exception as error:
            self.manager_error = f"manager heartbeat failed: {error}"

    def _status_payload(self) -> dict[str, Any]:
        native_pid = None
        if self.native_process is not None and self.native_process.poll() is None:
            native_pid = self.native_process.pid
        mapping_mb: Any = 290
        if self.reader is not None and self.reader.header is not None:
            mapping_mb = round(self.reader.header.total_bytes / (1024 * 1024), 1)

        r = self.readiness_details
        return {
            "provider_id": self.provider_id,
            "instance_id": self.instance_id,
            "boot_id": self.boot_id,
            "residency": self.residency,
            "health": self.health,
            "ready": self.ready,
            "pid": os.getpid(),
            "details": {
                "provider_version": "0.4.1",
                "native_pid": native_pid,
                "mapping_name": self.args.mapping_name,
                "last_error": self.last_error,
                "manager_error": self.manager_error,
                "required_data": {
                    "rgb": r["rgb"],
                    "depth": r["depth"],
                },
                "capability_readiness": {
                    "camera.rgb": r["rgb"],
                    "camera.depth": r["depth"],
                    "camera.ir": r["ir"],
                    "camera.depth_aligned_to_rgb": r["aligned_depth"],
                    "camera.point_cloud.xyz": r["point_cloud"]
                    and self.args.point_cloud_mode == "xyz",
                    "camera.point_cloud.xyzrgb.experimental": r["point_cloud"]
                    and self.args.point_cloud_mode == "xyzrgb",
                    "camera.imu.accel": r["accel"],
                    "camera.imu.gyro": r["gyro"],
                    "camera.imu.bundle": r["accel"] and r["gyro"],
                    "camera.imu.calibration": r["imu_calibration"],
                    "camera.ir_geometry": r["ir_geometry"],
                    "camera.frame_metadata": r["frame_metadata"],
                    "camera.global_timestamp": bool(
                        self.reader
                        and self.reader.header
                        and self.reader.header.global_timestamp_enabled
                    ),
                    "camera.rgbd_geometry": r["calibration"],
                    "camera.rgbd.synchronized": r["rgbd_sync"],
                    "camera.rgbd.bundle": r["rgbd_sync"],
                    GENERIC_RGBD_ROUTE_CAPABILITY: bool(
                        self.residency == "HOT" and r["rgb"] and r["depth"]
                    ),
                    DIRECT_RGBD_ROUTE_CAPABILITY: bool(
                        self.residency == "HOT" and r["rgb"] and r["depth"]
                    ),
                    "camera.device_info": r["device_info"],
                },
                "data_routes": self._rgbd_routes(),
                "route_publish_error": self.route_publish_error,
                "rgbd_synchronization": dict(self.latest_rgbd_sync_details),
                "configured_features": self._configured_features(),
                "calibration_revision": self.calibration_revision,
                "canonical_device_id": (
                    self.accelerometer_calibration.canonical_device_id
                    if self.accelerometer_calibration is not None
                    else None
                ),
                "accelerometer_calibration": (
                    {
                        "status": self.accelerometer_calibration.status,
                        "revision": self.accelerometer_calibration.revision,
                        "path": str(self.accelerometer_calibration.path),
                        "canonical_device_id": self.accelerometer_calibration.canonical_device_id,
                    }
                    if self.accelerometer_calibration is not None
                    else {
                        "status": "UNKNOWN_DEVICE_ID",
                        "error": self.device_identity_error,
                    }
                ),
                "resource_profile": {
                    "basis": "MEASURED" if self.reader is not None else "ESTIMATED",
                    "wrapper_ram_mb": 80,
                    "native_shared_memory_mb": mapping_mb,
                    "vram_mb": "NOT_APPLICABLE",
                    "cpu_cores_expected": 2.0 if self.args.point_cloud_mode != "off" else 1.0,
                    "usb_bandwidth": "UNKNOWN",
                    "cold_to_hot_ms": 7000,
                    "warm_to_hot_ms": 7000,
                },
            },
        }

    def _close_reader(self) -> None:
        if self.reader is not None:
            try:
                self.reader.close()
            finally:
                self.reader = None

    def _stop_native(self, force: bool) -> None:
        process = self.native_process
        if process is None:
            return
        if process.poll() is not None:
            self.native_process = None
            return
        if not force and os.name == "nt":
            try:
                os.kill(process.pid, signal.CTRL_BREAK_EVENT)
                process.wait(timeout=5)
            except Exception:
                force = True
        elif not force:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                force = True
        if force and process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        self.native_process = None

    def _pipe_native_logs(self) -> None:
        process = self.native_process
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            print(f"[CameraHost] {line.rstrip()}", flush=True)


class ControlHandler(BaseHTTPRequestHandler):
    provider: FemtoBoltProvider

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._reply(200, self.provider._status_payload())
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        try:
            if self.path == "/v1/control/hot":
                result = self.provider.start_hot()
            elif self.path == "/v1/control/warm":
                result = self.provider.enter_warm()
            elif self.path == "/v1/control/reload-calibration":
                result = self.provider.reload_calibration()
            elif self.path == "/v1/control/stop":
                result = self.provider.stop()
            else:
                self._reply(404, {"error": "not found"})
                return
            self._reply(200, result)
        except Exception as error:
            self.provider.health = "UNHEALTHY"
            self.provider.last_error = str(error)
            self._reply(500, {"error": str(error)})

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[CameraProviderControl] {format % args}", flush=True)

    def _reply(self, status: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manager-url", default="http://127.0.0.1:7001")
    parser.add_argument("--fabric-url", default="http://127.0.0.1:7002")
    parser.add_argument("--control-port", type=int, default=7101)
    parser.add_argument("--native-exe", required=True)
    parser.add_argument(
        "--mapping-name",
        default=r"Local\FemtoBoltPipeline_CameraHost_v2",
    )
    parser.add_argument("--camera-start-timeout", type=float, default=30.0)
    parser.add_argument("--poll-interval", type=float, default=0.02)
    parser.add_argument("--rgbd-max-delta-us", type=int, default=50_000)
    parser.add_argument("--optional-feature-grace", type=float, default=8.0)
    parser.add_argument(
        "--calibration-root",
        default=None,
        help="Persistent device calibration root; defaults to <workspace>/config/calibration/devices.",
    )
    parser.add_argument(
        "--workspace-root",
        default=os.getenv("PHYSICAL_AGENT_ROOT"),
        help="Workspace root used to resolve persistent configuration.",
    )
    parser.add_argument("--disable-ir", action="store_true")
    parser.add_argument("--disable-imu", action="store_true")
    parser.add_argument("--disable-frame-sync", action="store_true")
    parser.add_argument("--disable-hardware-d2c", action="store_true")
    parser.add_argument("--disable-aligned-depth", action="store_true")
    parser.add_argument(
        "--point-cloud-mode",
        choices=("off", "xyz", "xyzrgb"),
        default="xyz",
        help="xyzrgb is experimental with Orbbec SDK 2.8.6 on Femto Bolt.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    provider = FemtoBoltProvider(args)
    ControlHandler.provider = provider
    server = ThreadingHTTPServer(("127.0.0.1", args.control_port), ControlHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        return provider.run()
    except KeyboardInterrupt:
        provider.stop()
        return 130
    except Exception as error:
        provider.health = "UNHEALTHY"
        provider.last_error = str(error)
        print(f"[CameraProvider] fatal: {error}", file=sys.stderr, flush=True)
        return 1
    finally:
        server.shutdown()
        server.server_close()
        provider._close_reader()
        provider._stop_native(force=True)


if __name__ == "__main__":
    raise SystemExit(main())
