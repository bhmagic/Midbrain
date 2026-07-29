"""Local inertial-first visual-inertial Resource Provider.

The default backend propagates a 15-state inertial error-state filter from every
ordered IMU sample. RGB-D and optional synchronized IR/depth observations are
metric correction measurements. The Provider and Fabric contracts remain
backend-neutral so a native Basalt or OpenVINS-class adapter can replace this
Python reference backend without changing consumers.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

LOCAL_PYTHON_ROOT = Path(__file__).resolve().parent / "python"
if str(LOCAL_PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(LOCAL_PYTHON_ROOT))

import cv2
import httpx
import numpy as np
from PIL import Image

from local_vio_provider.math3d import make_transform, matrix_to_quaternion_xyzw
from local_vio_provider.prototype_backend import PoseResult
from local_vio_provider.inertial_first_backend import InertialFirstRgbdVio
from orbbec_femto_provider.shared_memory_access import (
    CameraSharedMemory,
    STREAM_ACCEL,
    STREAM_ALIGNED_DEPTH,
    STREAM_COLOR,
    STREAM_DEPTH,
    STREAM_GYRO,
    STREAM_IR,
)

BODY_FRAME = "body_base"
COLOR_FRAME = "femto_bolt_color_optical_frame"
IMU_FRAME = "femto_bolt_imu_frame"


class LocalVioProvider:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.provider_id = "localization.local_vio"
        self.instance_id = str(uuid.uuid4())
        self.boot_id = str(uuid.uuid4())
        self.shutdown_event = threading.Event()
        self.lock = threading.RLock()
        self.iteration_lock = threading.Lock()
        self.http = httpx.Client(timeout=5.0)
        self.residency = "WARM"
        self.health = "HEALTHY"
        self.ready = False
        self.last_error: Optional[str] = None
        self.manager_error: Optional[str] = None
        self.backend = InertialFirstRgbdVio(
            gravity_samples=args.gravity_samples,
            gravity_std_limit_mps2=args.gravity_std_limit_mps2,
            gravity_gyro_limit_radps=getattr(args, "gravity_gyro_limit_radps", 0.012),
            feature_preprocess_mode=getattr(
                args,
                "feature_preprocess_mode",
                "adaptive_circular_lcn",
            ),
            lcn_radius_px=getattr(args, "lcn_radius_px", 11),
            lcn_low_light_median=getattr(args, "lcn_low_light_median", 105.0),
            lcn_low_contrast_span=getattr(args, "lcn_low_contrast_span", 70.0),
            lcn_raw_keypoint_trigger=getattr(args, "lcn_raw_keypoint_trigger", 700),
            lcn_raw_inlier_accept=getattr(args, "lcn_raw_inlier_accept", 70),
            lcn_selection_margin=getattr(args, "lcn_selection_margin", 0.12),
            ir_enabled=getattr(args, "ir_enabled", True),
        )
        self.session_epoch = ""
        self.world_frame = ""
        self.sequence = 0
        self.last_rgb_frame = -1
        self.last_accel_frame = -1
        self.last_gyro_frame = -1
        self.imu_gap_count = 0
        self.reader: Optional[CameraSharedMemory] = None
        self.mapping_name: Optional[str] = None
        self.camera_calibration_revision: Optional[str] = None
        self.backend_configured = False
        self.accel_calibration_revision: Optional[str] = None
        self.accel_scale = np.ones(3, dtype=np.float64)
        self.accel_offset = np.zeros(3, dtype=np.float64)
        self.color_from_imu = np.eye(4, dtype=np.float64)
        self.origin_translation_adjustment: Optional[np.ndarray] = None
        self.last_tracking_state = "STOPPED"
        self.last_result: Optional[PoseResult] = None
        self.motion_inhibited = False
        self.last_static_transform_epoch: Optional[str] = None
        self.last_inertial_prediction_us = -1
        self.last_inertial_publish_monotonic = 0.0
        self.cached_inputs: dict[str, tuple[float, Optional[dict[str, Any]]]] = {}
        self._reset_session("provider_boot")

    def register(self) -> None:
        response = self.http.post(
            f"{self.args.manager_url}/v1/providers/register",
            json=self._status_payload(),
        )
        response.raise_for_status()

    def start_hot(self) -> dict[str, Any]:
        with self.iteration_lock:
            with self.lock:
                already_hot = self.residency == "HOT"
                self.residency = "HOT"
                self.health = "HEALTHY"
                self.last_error = None
                if not already_hot:
                    self._reset_session("hot_start")
                self._heartbeat()
                return {
                    "status": "already_hot" if already_hot else "hot",
                    "backend": self.args.backend,
                    "session_epoch": self.session_epoch,
                    "world_frame": self.world_frame,
                }

    def enter_warm(self) -> dict[str, Any]:
        with self.lock:
            self.residency = "WARM"
            self.ready = False
            self.last_tracking_state = "WARM"
            self._close_reader()
            return {"status": "warm"}

    def stop(self) -> dict[str, Any]:
        self.shutdown_event.set()
        with self.lock:
            self.residency = "STOPPING"
            self.ready = False
            self.last_tracking_state = "STOPPING"
            self._close_reader()
        return {"status": "stopping"}

    def handle_request(self, request: dict[str, Any]) -> dict[str, Any]:
        action = str(request.get("action") or "").strip().lower()
        if action in {"reset", "initialize", "force_reset"}:
            with self.iteration_lock:
                with self.lock:
                    self._reset_session(action)
                    response = {
                        "status": "reset",
                        "session_epoch": self.session_epoch,
                        "world_frame": self.world_frame,
                        "related_skill_id": request.get("related_skill_id"),
                    }
                # Reset acceptance must not depend on an immediate diagnostic
                # publication. A transient Fabric failure used to turn a completed
                # reset into HTTP 500, causing the Skill to release motion inhibit.
                try:
                    self._publish_status("INITIALIZING", f"VIO session reset: {action}")
                except Exception as error:
                    warning = f"reset accepted; immediate status publish failed: {error}"
                    self.last_error = warning
                    response["status_publish_warning"] = warning
                return response
        if action == "status":
            return self._status_payload()
        raise ValueError(f"unsupported VIO action: {action or 'empty'}")

    def _reset_session(self, reason: str) -> None:
        self.session_epoch = str(uuid.uuid4())
        self.world_frame = f"local_vio/{self.session_epoch}"
        # Sequence is scoped to provider instance + boot ID and must never move
        # backward. Resetting it caused Fabric to reject every post-reset update.
        self.last_rgb_frame = -1
        self.last_accel_frame = -1
        self.last_gyro_frame = -1
        self.imu_gap_count = 0
        self.origin_translation_adjustment = None
        self.last_static_transform_epoch = None
        self.last_inertial_prediction_us = -1
        self.last_inertial_publish_monotonic = 0.0
        self._close_reader()
        self.backend.reset()
        self.ready = False
        self.last_tracking_state = "INITIALIZING"
        self.last_result = None
        self.last_error = f"VIO session reset: {reason}"

    def run(self) -> int:
        self.register()
        self.start_hot()
        heartbeat_at = 0.0
        while not self.shutdown_event.is_set():
            now = time.monotonic()
            if now >= heartbeat_at:
                self._heartbeat()
                heartbeat_at = now + 1.0
            if self.residency != "HOT":
                time.sleep(0.1)
                continue
            try:
                with self.iteration_lock:
                    self._iteration()
            except Exception as error:
                self.health = "DEGRADED"
                self.last_error = str(error)
                time.sleep(0.1)
        self._close_reader()
        return 0

    def _iteration(self) -> None:
        calibration_observation = self._latest_cached(
            "camera.calibration", refresh_s=1.0
        )
        accel_calibration_observation = self._latest_cached(
            "camera.imu.accel.calibration", refresh_s=1.0
        )
        motion_observation = self._latest_cached(
            "system.motion.inhibit", refresh_s=0.05
        )
        bundle_observation = self._latest_cached(
            "camera.rgbd.bundle",
            refresh_s=1.0,
        )
        if calibration_observation is None or bundle_observation is None:
            self._publish_status("WAITING_FOR_INPUTS", "camera calibration or RGB-D bundle unavailable")
            time.sleep(self.args.poll_interval)
            return

        self._configure_calibration(calibration_observation)
        self._configure_accelerometer(accel_calibration_observation)
        self.motion_inhibited = bool(
            motion_observation
            and (motion_observation.get("data") or {}).get("inhibited", False)
        )

        bundle = bundle_observation.get("data") or {}
        rgb_reference = bundle.get("rgb")
        aligned_reference = bundle.get("depth_aligned_to_rgb")
        if not isinstance(rgb_reference, dict) or not isinstance(aligned_reference, dict):
            self._publish_status("WAITING_FOR_INPUTS", "aligned RGB-D bundle unavailable")
            time.sleep(self.args.poll_interval)
            return

        self._ensure_reader(str(rgb_reference.get("mapping_name") or ""))
        self._consume_imu()
        (
            rgb_reference,
            aligned_reference,
            rgb,
            depth_m,
        ) = self._read_latest_rgbd(
            maximum_delta_us=int(bundle.get("max_delta_us") or 50_000)
        )
        frame_number = int(rgb_reference.get("frame_number", -1))
        if frame_number <= self.last_rgb_frame:
            self._publish_inertial_prediction_if_due()
            time.sleep(self.args.poll_interval)
            return

        timestamp_us = self._reference_timestamp(rgb_reference)

        ir_gray = None
        ir_depth_m = None
        ir_timestamp_us = None
        if getattr(self.args, "ir_enabled", True):
            try:
                reader = self._require_reader()
                ir_buffer_ref = reader.latest_ref(STREAM_IR)
                native_depth_buffer_ref = reader.latest_ref(STREAM_DEPTH)
                ir_reference = (
                    ir_buffer_ref.to_dict()
                    if ir_buffer_ref is not None
                    else None
                )
                native_depth_reference = (
                    native_depth_buffer_ref.to_dict()
                    if native_depth_buffer_ref is not None
                    else None
                )
                if isinstance(ir_reference, dict) and isinstance(native_depth_reference, dict):
                    ir_time = self._reference_timestamp(ir_reference)
                    depth_time = self._reference_timestamp(native_depth_reference)
                    tolerance = int(getattr(self.args, "ir_sync_tolerance_us", 8_000))
                    rgb_time = int(timestamp_us)
                    if (
                        abs(ir_time - depth_time) <= tolerance
                        and abs(ir_time - rgb_time) <= tolerance
                    ):
                        ir_gray = self._read_ir_u8(ir_reference)
                        ir_depth_m = self._read_depth_m(native_depth_reference)
                        if ir_depth_m.shape != ir_gray.shape:
                            ir_depth_m = cv2.resize(
                                ir_depth_m,
                                (ir_gray.shape[1], ir_gray.shape[0]),
                                interpolation=cv2.INTER_NEAREST,
                            )
                        ir_timestamp_us = ir_time
            except Exception:
                # IR is an optional low-light correction source. RGB-D and IMU
                # continue even when the current IR BufferRef has been recycled.
                ir_gray = None
                ir_depth_m = None
                ir_timestamp_us = None

        result = self.backend.process(
            rgb,
            depth_m,
            timestamp_us,
            ir_gray=ir_gray,
            ir_depth_m=ir_depth_m,
            ir_timestamp_us=ir_timestamp_us,
        )
        self.last_rgb_frame = frame_number
        self.last_result = result
        self.last_tracking_state = result.tracking_state
        self.ready = result.tracking_state == "TRACKING"
        self.health = "HEALTHY" if self.ready else "DEGRADED"
        self.last_error = result.message
        self._publish_result(result)
        self.last_inertial_prediction_us = max(
            self.last_inertial_prediction_us,
            int(result.timestamp_us),
        )
        self.last_inertial_publish_monotonic = time.monotonic()
        time.sleep(self.args.poll_interval)

    def _read_latest_rgbd(
        self,
        *,
        maximum_delta_us: int,
    ) -> tuple[dict[str, Any], dict[str, Any], np.ndarray, np.ndarray]:
        """Copy the newest provider-local RGB-D pair before its ring recycles."""

        reader = self._require_reader()
        last_error: Exception | None = None
        for _ in range(4):
            try:
                aligned_buffer_ref = reader.latest_ref(
                    STREAM_ALIGNED_DEPTH
                )
                if aligned_buffer_ref is None:
                    raise RuntimeError(
                        "shared memory has no aligned-depth frame"
                    )
                aligned_reference = aligned_buffer_ref.to_dict()
                depth_m = self._read_depth_m(aligned_reference)

                rgb_buffer_ref = reader.latest_ref(STREAM_COLOR)
                if rgb_buffer_ref is None:
                    raise RuntimeError("shared memory has no RGB frame")
                rgb_reference = rgb_buffer_ref.to_dict()
                rgb = self._read_rgb(rgb_reference)
            except RuntimeError as error:
                last_error = error
                continue

            rgb_timestamp_us = self._reference_timestamp(rgb_reference)
            aligned_timestamp_us = self._reference_timestamp(
                aligned_reference
            )
            if min(rgb_timestamp_us, aligned_timestamp_us) <= 0:
                last_error = RuntimeError(
                    "provider-local RGB-D references have no usable timestamp"
                )
                continue
            if (
                maximum_delta_us > 0
                and abs(aligned_timestamp_us - rgb_timestamp_us)
                > maximum_delta_us
            ):
                last_error = RuntimeError(
                    "provider-local RGB and aligned depth exceed the declared "
                    "synchronization threshold"
                )
                continue
            return rgb_reference, aligned_reference, rgb, depth_m
        raise RuntimeError(
            "could not copy a fresh provider-local synchronized RGB-D pair: "
            f"{last_error}"
        )

    def _publish_inertial_prediction_if_due(self) -> None:
        publish_hz = max(1.0, float(getattr(self.args, "inertial_publish_hz", 100.0)))
        now = time.monotonic()
        if now - self.last_inertial_publish_monotonic < 1.0 / publish_hz:
            return
        result = self.backend.predict_latest()
        if result is None or result.timestamp_us <= self.last_inertial_prediction_us:
            return
        self.last_result = result
        self.last_tracking_state = result.tracking_state
        self.ready = result.tracking_state == "TRACKING"
        self.health = "HEALTHY" if self.ready else "DEGRADED"
        self.last_error = result.message
        self._publish_result(result)
        self.last_inertial_prediction_us = int(result.timestamp_us)
        self.last_inertial_publish_monotonic = now

    def _configure_calibration(self, observation: dict[str, Any]) -> None:
        revision = observation.get("calibration_revision")
        data = observation.get("data") or {}
        rgb = data.get("rgb_intrinsic") or {}
        camera_matrix = np.array(
            [
                [float(rgb.get("fx", 0.0)), 0.0, float(rgb.get("cx", 0.0))],
                [0.0, float(rgb.get("fy", 0.0)), float(rgb.get("cy", 0.0))],
                [0.0, 0.0, 1.0],
            ],
            dtype=np.float64,
        )
        if camera_matrix[0, 0] <= 0.0 or camera_matrix[1, 1] <= 0.0:
            raise RuntimeError("RGB camera intrinsics are invalid")

        color_from_imu = np.eye(4, dtype=np.float64)
        imu = data.get("imu") or {}
        extrinsic = imu.get("accelerometer_to_color")
        if isinstance(extrinsic, dict):
            rotation = np.asarray(extrinsic.get("rot"), dtype=np.float64)
            translation = np.asarray(extrinsic.get("trans"), dtype=np.float64)
            if rotation.shape == (9,) and translation.shape == (3,):
                color_from_imu = make_transform(
                    rotation.reshape(3, 3),
                    translation / 1000.0,
                )

        ir_camera_matrix = None
        color_from_ir = None
        infrared = data.get("infrared") or {}
        ir_intrinsic = infrared.get("intrinsic") or {}
        if all(float(ir_intrinsic.get(field, 0.0) or 0.0) > 0.0 for field in ("fx", "fy")):
            ir_camera_matrix = np.array(
                [
                    [float(ir_intrinsic.get("fx")), 0.0, float(ir_intrinsic.get("cx", 0.0))],
                    [0.0, float(ir_intrinsic.get("fy")), float(ir_intrinsic.get("cy", 0.0))],
                    [0.0, 0.0, 1.0],
                ],
                dtype=np.float64,
            )
        ir_to_color = infrared.get("to_color")
        if isinstance(ir_to_color, dict):
            rotation = np.asarray(ir_to_color.get("rot"), dtype=np.float64)
            translation = np.asarray(ir_to_color.get("trans"), dtype=np.float64)
            if rotation.shape == (9,) and translation.shape == (3,):
                color_from_ir = make_transform(
                    rotation.reshape(3, 3),
                    translation / 1000.0,
                )

        accel_intrinsic = ((imu.get("accelerometer") or {}).get("intrinsic") or {})
        gyro_intrinsic = ((imu.get("gyroscope") or {}).get("intrinsic") or {})
        imu_noise = {}
        for target, source, fallback in (
            ("accel_noise_density", accel_intrinsic.get("noise_density"), 0.08),
            ("accel_random_walk", accel_intrinsic.get("random_walk"), 0.002),
            ("gyro_noise_density", gyro_intrinsic.get("noise_density"), 0.008),
            ("gyro_random_walk", gyro_intrinsic.get("random_walk"), 0.0002),
        ):
            try:
                value = float(source)
                if not math.isfinite(value) or value <= 0.0:
                    value = fallback
            except (TypeError, ValueError):
                value = fallback
            imu_noise[target] = value

        if not self.backend_configured or revision != self.camera_calibration_revision:
            self.camera_calibration_revision = str(revision) if revision else None
            self.color_from_imu = color_from_imu
            self.backend.configure(
                camera_matrix,
                color_from_imu,
                ir_camera_matrix=ir_camera_matrix,
                color_from_ir=color_from_ir,
                imu_noise=imu_noise,
            )
            self.backend_configured = True

    def _configure_accelerometer(self, observation: Optional[dict[str, Any]]) -> None:
        if observation is None:
            self.accel_scale = np.ones(3, dtype=np.float64)
            self.accel_offset = np.zeros(3, dtype=np.float64)
            self.accel_calibration_revision = None
            return
        data = observation.get("data") or {}
        correction = data.get("correction") or {}
        scale = np.asarray(correction.get("scale", [1.0, 1.0, 1.0]), dtype=np.float64)
        offset = np.asarray(correction.get("offset", [0.0, 0.0, 0.0]), dtype=np.float64)
        if scale.shape != (3,) or offset.shape != (3,):
            raise RuntimeError("accelerometer calibration must contain three scale and offset values")
        self.accel_scale = scale
        self.accel_offset = offset
        self.accel_calibration_revision = observation.get("calibration_revision")

    def _ensure_reader(self, mapping_name: str) -> None:
        if not mapping_name:
            raise RuntimeError("RGB BufferRef does not include a mapping name")
        if self.reader is not None and self.mapping_name == mapping_name:
            return
        self._close_reader()
        self.reader = CameraSharedMemory(mapping_name).open()
        self.mapping_name = mapping_name

    def _consume_imu(self) -> None:
        reader = self.reader
        if reader is None:
            return
        accel_samples = reader.recent_imu_samples(
            STREAM_ACCEL,
            after_frame_number=self.last_accel_frame,
        )
        gyro_samples = reader.recent_imu_samples(
            STREAM_GYRO,
            after_frame_number=self.last_gyro_frame,
        )
        for sample in accel_samples:
            if self.last_accel_frame >= 0 and sample.frame_number > self.last_accel_frame + 1:
                self.imu_gap_count += sample.frame_number - self.last_accel_frame - 1
            corrected = self.accel_scale * np.array([sample.x, sample.y, sample.z]) + self.accel_offset
            self.backend.add_accelerometer(
                self._sample_timestamp(sample),
                corrected,
                motion_inhibited=self.motion_inhibited,
            )
            self.last_accel_frame = max(self.last_accel_frame, sample.frame_number)
        for sample in gyro_samples:
            if self.last_gyro_frame >= 0 and sample.frame_number > self.last_gyro_frame + 1:
                self.imu_gap_count += sample.frame_number - self.last_gyro_frame - 1
            self.backend.add_gyroscope(
                self._sample_timestamp(sample),
                np.array([sample.x, sample.y, sample.z], dtype=np.float64),
            )
            self.last_gyro_frame = max(self.last_gyro_frame, sample.frame_number)

    def _publish_result(self, result: PoseResult) -> None:
        observations: list[dict[str, Any]] = []
        self.sequence += 1
        if self.last_static_transform_epoch != self.session_epoch:
            self.last_static_transform_epoch = self.session_epoch
            observations.append(
                self._observation(
                    stream="transform.body_from_imu",
                    schema="physical_agent.transform",
                    sequence=self.sequence,
                    observed_at_us=result.timestamp_us,
                    coordinate_frame=BODY_FRAME,
                    calibration_revision=self.camera_calibration_revision,
                    data={
                        "parent_frame": BODY_FRAME,
                        "child_frame": IMU_FRAME,
                        "translation_m": [0.0, 0.0, 0.0],
                        "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
                        "is_static": True,
                        "authority": f"{self.provider_id}:fused_head_body",
                        "session_epoch": None,
                        "continuity": "STATIC",
                    },
                    freshness_ms=None,
                )
            )

        raw_world_from_body = result.world_from_camera @ self.color_from_imu
        if self.origin_translation_adjustment is None and result.tracking_state == "TRACKING":
            adjustment = np.eye(4, dtype=np.float64)
            adjustment[:3, 3] = -raw_world_from_body[:3, 3]
            self.origin_translation_adjustment = adjustment
        adjustment = (
            self.origin_translation_adjustment
            if self.origin_translation_adjustment is not None
            else np.eye(4, dtype=np.float64)
        )
        world_from_body = adjustment @ raw_world_from_body
        world_from_camera = adjustment @ result.world_from_camera
        position = world_from_body[:3, 3]
        orientation = matrix_to_quaternion_xyzw(world_from_body[:3, :3])

        status_data = self._status_data(result)
        observations.append(
            self._observation(
                stream="localization.vio.status",
                schema="physical_agent.vio_status",
                sequence=self.sequence,
                observed_at_us=result.timestamp_us,
                coordinate_frame=self.world_frame,
                calibration_revision=self.camera_calibration_revision,
                data=status_data,
                freshness_ms=1000,
            )
        )

        publish_pose = (
            self.origin_translation_adjustment is not None
            and result.tracking_state in {"TRACKING", "DEGRADED"}
        )
        if publish_pose:
            pose_data = {
                "world_frame": self.world_frame,
                "body_frame": BODY_FRAME,
                "camera_frame": COLOR_FRAME,
                "session_epoch": self.session_epoch,
                "position_m": position.tolist(),
                "orientation_xyzw": orientation.tolist(),
                "linear_velocity_world_mps": result.velocity_world_mps.tolist(),
                "world_from_camera": {
                    "translation_m": world_from_camera[:3, 3].tolist(),
                    "rotation_xyzw": matrix_to_quaternion_xyzw(
                        world_from_camera[:3, :3]
                    ).tolist(),
                },
                "covariance_6x6": self._result_covariance(result),
                "backend": self.args.backend,
                "backend_classification": "INERTIAL_FIRST_ERROR_STATE_FILTER_WITH_RGBD_VISUAL_UPDATES_AND_OPTIONAL_IR_FALLBACK",
                "tracking_state": result.tracking_state,
                "rotation_source": result.rotation_source,
                "rotation_disagreement_rad": result.rotation_disagreement_rad,
                "gyro_rotation_sample_count": result.gyro_rotation_sample_count,
                "gyro_rotation_angle_rad": result.gyro_rotation_angle_rad,
                "pose_quality": (
                    "INERTIAL_PROPAGATION_VISUALLY_CORRECTED"
                    if result.visual_update_accepted
                    else "INERTIAL_PROPAGATION_RECENT_VISUAL"
                    if result.tracking_state == "TRACKING"
                    else "INERTIAL_PROPAGATION_VISUAL_STALE"
                ),
                "pose_update_mode": result.pose_update_mode,
                "visual_update_accepted": result.visual_update_accepted,
                "visual_sensor": result.visual_sensor,
                "visual_reprojection_rmse_px": result.visual_reprojection_rmse_px,
                "visual_correction_position_m": result.visual_correction_position_m,
                "visual_correction_rotation_rad": result.visual_correction_rotation_rad,
                "visual_stale_s": result.visual_stale_s,
                "imu_propagation_steps": result.imu_propagation_steps,
                "imu_state_timestamp_us": result.imu_state_timestamp_us,
                "filter_position_std_m": list(result.filter_position_std_m),
                "filter_rotation_std_rad": list(result.filter_rotation_std_rad),
                "world_up_axis": [0.0, 1.0, 0.0],
                "world_down_axis": [0.0, -1.0, 0.0],
                "gravity_stabilization": {
                    "correction_applied": result.gravity_correction_applied,
                    "tilt_error_rad": result.gravity_tilt_error_rad,
                    "tracking_sample_count": result.gravity_tracking_sample_count,
                    "direction_std_rad": result.gravity_direction_std_rad,
                    "stationary_duration_s": result.gravity_stationary_duration_s,
                    "mode": result.gravity_correction_mode,
                    "adjustment_state": result.gravity_adjustment_state,
                    "gyro_configured_limit_radps": self.backend.gravity_gyro_limit_radps,
                    "gyro_effective_limit_radps": result.gravity_gyro_effective_limit_radps,
                    "gyro_noise_floor_radps": result.gravity_gyro_noise_floor_radps,
                    "gyro_rms_radps": result.gravity_gyro_rms_radps,
                    "gyro_p95_radps": result.gravity_gyro_p95_radps,
                    "gyro_bias_imu_radps": self.backend.gyro_bias_imu_radps.tolist(),
                },
                "feature_tracking": {
                    "selected_mode": result.feature_preprocess_mode,
                    "selected_sensor": result.visual_sensor,
                    "raw_keypoint_count": result.raw_keypoint_count,
                    "normalized_keypoint_count": result.normalized_keypoint_count,
                    "frame_luma_median": result.frame_luma_median,
                    "ir_keypoint_count": result.ir_keypoint_count,
                    "ir_inlier_count": result.ir_inlier_count,
                    "ir_frame_luma_median": result.ir_frame_luma_median,
                },
            }
            observations.extend(
                [
                    self._observation(
                        stream="localization.body.pose",
                        schema="physical_agent.pose_estimate",
                        sequence=self.sequence,
                        observed_at_us=result.timestamp_us,
                        coordinate_frame=self.world_frame,
                        calibration_revision=self.camera_calibration_revision,
                        data=pose_data,
                        freshness_ms=500,
                    ),
                    self._observation(
                        stream="transform.local_vio.body",
                        schema="physical_agent.transform",
                        sequence=self.sequence,
                        observed_at_us=result.timestamp_us,
                        coordinate_frame=self.world_frame,
                        calibration_revision=self.camera_calibration_revision,
                        data={
                            "parent_frame": self.world_frame,
                            "child_frame": BODY_FRAME,
                            "translation_m": position.tolist(),
                            "rotation_xyzw": orientation.tolist(),
                            "is_static": False,
                            "authority": f"{self.provider_id}:{self.instance_id}",
                            "session_epoch": self.session_epoch,
                            "covariance_6x6": self._result_covariance(result),
                            "continuity": "CONTINUOUS_INERTIAL_PROPAGATION_WITHIN_EPOCH",
                            "tracking_state": result.tracking_state,
                        },
                        freshness_ms=500,
                    ),
                    self._observation(
                        stream="localization.vio.bias",
                        schema="physical_agent.vio_bias",
                        sequence=self.sequence,
                        observed_at_us=result.timestamp_us,
                        coordinate_frame=IMU_FRAME,
                        calibration_revision=self.accel_calibration_revision,
                        data={
                            "session_epoch": self.session_epoch,
                            "estimated": True,
                            "accelerometer_bias_m_s2": list(result.estimated_accel_bias_mps2),
                            "gyroscope_bias_rad_s": list(result.estimated_gyro_bias_radps),
                            "source": "startup zero-rate estimate plus error-state visual corrections and quiet-window refinement",
                        },
                        freshness_ms=1000,
                    ),
                ]
            )

        response = self.http.post(
            f"{self.args.fabric_url}/v1/observations/batch",
            json={"observations": observations},
        )
        response.raise_for_status()

    def _publish_status(self, state: str, message: str) -> None:
        now_us = int(time.time() * 1_000_000)
        self.sequence += 1
        result = PoseResult(
            timestamp_us=now_us,
            world_from_camera=np.eye(4, dtype=np.float64),
            velocity_world_mps=np.zeros(3, dtype=np.float64),
            tracking_state=state,
            inlier_count=0,
            match_count=0,
            translation_step_m=0.0,
            rotation_step_rad=0.0,
            gravity_sample_count=len(self.backend.acceleration_samples),
            gravity_std_mps2=self.backend.gravity_std_mps2,
            gyro_delta_rad=None,
            gravity_tracking_sample_count=self.backend.last_gravity_tracking_sample_count,
            gravity_correction_applied=self.backend.last_gravity_correction_applied,
            gravity_tilt_error_rad=self.backend.last_gravity_tilt_error_rad,
            gravity_direction_std_rad=self.backend.last_gravity_direction_std_rad,
            gravity_stationary_duration_s=self.backend.last_gravity_stationary_duration_s,
            gravity_correction_mode=self.backend.last_gravity_correction_mode,
            gravity_adjustment_state=self.backend.last_gravity_adjustment_state,
            gravity_gyro_rms_radps=self.backend.last_gravity_gyro_rms_radps,
            gravity_gyro_p95_radps=self.backend.last_gravity_gyro_p95_radps,
            gravity_gyro_noise_floor_radps=self.backend.gravity_gyro_noise_floor_radps,
            gravity_gyro_effective_limit_radps=self.backend.gravity_gyro_effective_limit_radps,
            rotation_source=self.backend.last_rotation_source,
            rotation_disagreement_rad=self.backend.last_rotation_disagreement_rad,
            gyro_rotation_sample_count=self.backend.last_gyro_rotation_sample_count,
            gyro_rotation_angle_rad=self.backend.last_gyro_rotation_angle_rad,
            feature_preprocess_mode=self.backend.last_feature_preprocess_mode,
            raw_keypoint_count=self.backend.last_raw_keypoint_count,
            normalized_keypoint_count=self.backend.last_normalized_keypoint_count,
            frame_luma_median=self.backend.last_frame_luma_median,
            message=message,
        )
        observation = self._observation(
            stream="localization.vio.status",
            schema="physical_agent.vio_status",
            sequence=self.sequence,
            observed_at_us=now_us,
            coordinate_frame=self.world_frame,
            calibration_revision=self.camera_calibration_revision,
            data=self._status_data(result),
            freshness_ms=1000,
        )
        response = self.http.post(
            f"{self.args.fabric_url}/v1/observations",
            json=observation,
        )
        response.raise_for_status()

    def _status_data(self, result: PoseResult) -> dict[str, Any]:
        return {
            "tracking_state": result.tracking_state,
            "ready": result.tracking_state == "TRACKING",
            "session_epoch": self.session_epoch,
            "world_frame": self.world_frame,
            "body_frame": BODY_FRAME,
            "camera_frame": COLOR_FRAME,
            "backend": self.args.backend,
            "backend_classification": "INERTIAL_FIRST_ERROR_STATE_FILTER_WITH_RGBD_VISUAL_UPDATES_AND_OPTIONAL_IR_FALLBACK",
            "production_backend_target": "openvins_or_basalt_native_adapter",
            "motion_inhibited": self.motion_inhibited,
            "gravity_sample_count": result.gravity_sample_count,
            "gravity_samples_required": self.args.gravity_samples,
            "gravity_std_mps2": result.gravity_std_mps2,
            "gravity_tracking_sample_count": result.gravity_tracking_sample_count,
            "gravity_correction_applied": result.gravity_correction_applied,
            "gravity_tilt_error_rad": result.gravity_tilt_error_rad,
            "gravity_direction_std_rad": result.gravity_direction_std_rad,
            "gravity_stationary_duration_s": result.gravity_stationary_duration_s,
            "gravity_correction_mode": result.gravity_correction_mode,
            "gravity_adjustment_state": result.gravity_adjustment_state,
            "gravity_affects_pose": result.gravity_correction_applied,
            "gravity_gyro_configured_limit_radps": self.backend.gravity_gyro_limit_radps,
            "gravity_gyro_effective_limit_radps": result.gravity_gyro_effective_limit_radps,
            "gravity_gyro_noise_floor_radps": result.gravity_gyro_noise_floor_radps,
            "gravity_gyro_rms_radps": result.gravity_gyro_rms_radps,
            "gravity_gyro_p95_radps": result.gravity_gyro_p95_radps,
            "gravity_gyro_bias_imu_radps": self.backend.gyro_bias_imu_radps.tolist(),
            "gravity_can_adjust_while_tracking": True,
            "pose_update_mode": result.pose_update_mode,
            "visual_update_accepted": result.visual_update_accepted,
            "visual_sensor": result.visual_sensor,
            "visual_reprojection_rmse_px": result.visual_reprojection_rmse_px,
            "visual_correction_position_m": result.visual_correction_position_m,
            "visual_correction_rotation_rad": result.visual_correction_rotation_rad,
            "visual_stale_s": result.visual_stale_s,
            "imu_propagation_steps": result.imu_propagation_steps,
            "imu_state_timestamp_us": result.imu_state_timestamp_us,
            "estimated_gyroscope_bias_radps": list(result.estimated_gyro_bias_radps),
            "estimated_accelerometer_bias_mps2": list(result.estimated_accel_bias_mps2),
            "filter_position_std_m": list(result.filter_position_std_m),
            "filter_rotation_std_rad": list(result.filter_rotation_std_rad),
            "world_up_axis": [0.0, 1.0, 0.0],
            "world_down_axis": [0.0, -1.0, 0.0],
            "visual_inlier_count": result.inlier_count,
            "visual_match_count": result.match_count,
            "feature_preprocess_mode": result.feature_preprocess_mode,
            "feature_raw_keypoint_count": result.raw_keypoint_count,
            "feature_normalized_keypoint_count": result.normalized_keypoint_count,
            "frame_luma_median": result.frame_luma_median,
            "ir_keypoint_count": result.ir_keypoint_count,
            "ir_inlier_count": result.ir_inlier_count,
            "ir_frame_luma_median": result.ir_frame_luma_median,
            "translation_step_m": result.translation_step_m,
            "rotation_step_rad": result.rotation_step_rad,
            "gyro_delta_rad": result.gyro_delta_rad,
            "rotation_source": result.rotation_source,
            "rotation_disagreement_rad": result.rotation_disagreement_rad,
            "gyro_rotation_sample_count": result.gyro_rotation_sample_count,
            "gyro_rotation_angle_rad": result.gyro_rotation_angle_rad,
            "rotation_trusted": result.tracking_state == "TRACKING",
            "inertial_first": True,
            "visual_is_measurement_update": True,
            "depth_aided_metric_update": True,
            "ir_fallback_enabled": bool(getattr(self.args, "ir_enabled", True)),
            "imu_gap_count": self.imu_gap_count,
            "imu_accelerometer_history_count": result.imu_accelerometer_history_count,
            "imu_gyroscope_history_count": result.imu_gyroscope_history_count,
            "imu_timestamp_skew_us": result.imu_timestamp_skew_us,
            "initialization_blocker": result.initialization_blocker,
            "initialization_accelerometer_window_count": (
                result.initialization_accelerometer_window_count
            ),
            "initialization_gyroscope_window_count": (
                result.initialization_gyroscope_window_count
            ),
            "initialization_accelerometer_rate_hz": (
                result.initialization_accelerometer_rate_hz
            ),
            "initialization_gyroscope_rate_hz": (
                result.initialization_gyroscope_rate_hz
            ),
            "camera_calibration_revision": self.camera_calibration_revision,
            "accelerometer_calibration_revision": self.accel_calibration_revision,
            "message": result.message,
        }

    @staticmethod
    def _result_covariance(result: PoseResult) -> list[float]:
        if len(result.covariance_6x6) == 36:
            return [float(value) for value in result.covariance_6x6]
        return LocalVioProvider._heuristic_covariance(result)

    @staticmethod
    def _heuristic_covariance(result: PoseResult) -> list[float]:
        quality_multiplier = 25.0 if result.tracking_state != "TRACKING" else 1.0
        position_variance = quality_multiplier * max(0.0004, 0.03 / max(1, result.inlier_count))
        rotation_variance = quality_multiplier * max(0.0001, 0.01 / max(1, result.inlier_count))
        covariance = [0.0] * 36
        covariance[0] = position_variance
        covariance[7] = position_variance
        covariance[14] = position_variance
        covariance[21] = rotation_variance
        covariance[28] = rotation_variance
        covariance[35] = rotation_variance
        return covariance

    def _latest_cached(
        self,
        stream: str,
        *,
        refresh_s: float,
    ) -> Optional[dict[str, Any]]:
        now = time.monotonic()
        cached = self.cached_inputs.get(stream)
        if cached is not None and now - cached[0] < max(0.0, float(refresh_s)):
            return cached[1]
        value = self._latest_optional(stream)
        self.cached_inputs[stream] = (now, value)
        return value

    def _latest_optional(self, stream: str) -> Optional[dict[str, Any]]:
        encoded = quote(stream, safe="")
        response = self.http.get(f"{self.args.fabric_url}/v1/latest/{encoded}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def _read_rgb(self, reference: dict[str, Any]) -> np.ndarray:
        reader = self._require_reader()
        payload = reader.read_ref(reference)
        format_name = str(reference.get("format_name", "")).upper()
        width = int(reference["width"])
        height = int(reference["height"])
        if format_name in {"MJPG", "MJPEG", "JPEG", "JPG"}:
            decoded = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
            if decoded is None:
                raise RuntimeError("OpenCV could not decode the RGB JPEG frame")
            return cv2.cvtColor(decoded, cv2.COLOR_BGR2RGB)
        if format_name == "RGB":
            return np.frombuffer(payload, dtype=np.uint8).reshape(height, width, 3).copy()
        if format_name == "BGR":
            bgr = np.frombuffer(payload, dtype=np.uint8).reshape(height, width, 3)
            return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        if format_name == "RGBA":
            image = Image.frombytes("RGBA", (width, height), payload)
            return np.asarray(image.convert("RGB"))
        if format_name == "BGRA":
            image = Image.frombytes("RGBA", (width, height), payload, "raw", "BGRA")
            return np.asarray(image.convert("RGB"))
        raise RuntimeError(f"unsupported RGB format for VIO: {format_name or 'unknown'}")

    def _read_ir_u8(self, reference: dict[str, Any]) -> np.ndarray:
        reader = self._require_reader()
        payload = reader.read_ref(reference)
        format_name = str(reference.get("format_name", "")).upper()
        width = int(reference["width"])
        height = int(reference["height"])
        expected = width * height
        if format_name in {"Y16", "IR16", "GRAY16", "DEPTH16", "Z16"}:
            values = np.frombuffer(payload, dtype="<u2", count=expected)
            if values.size != expected:
                raise RuntimeError("IR payload is shorter than declared dimensions")
            image = values.reshape(height, width).astype(np.float32)
            valid = image[image > 0.0]
            if valid.size < 64:
                return np.zeros((height, width), dtype=np.uint8)
            low, high = np.percentile(valid, [2.0, 98.0])
            if high <= low + 1.0:
                high = low + 1.0
            normalized = np.clip((image - low) * (255.0 / (high - low)), 0.0, 255.0)
            return normalized.astype(np.uint8)
        if format_name in {"Y8", "GRAY8", "MONO8"}:
            values = np.frombuffer(payload, dtype=np.uint8, count=expected)
            if values.size != expected:
                raise RuntimeError("IR payload is shorter than declared dimensions")
            return values.reshape(height, width).copy()
        raise RuntimeError(f"unsupported IR format for VIO: {format_name or 'unknown'}")

    def _read_depth_m(self, reference: dict[str, Any]) -> np.ndarray:
        reader = self._require_reader()
        payload = reader.read_ref(reference)
        format_name = str(reference.get("format_name", "")).upper()
        if format_name not in {"Y16", "DEPTH16", "Z16"}:
            raise RuntimeError(f"unsupported aligned depth format for VIO: {format_name or 'unknown'}")
        width = int(reference["width"])
        height = int(reference["height"])
        expected = width * height
        values = np.frombuffer(payload, dtype="<u2", count=expected)
        if values.size != expected:
            raise RuntimeError("aligned depth payload is shorter than declared dimensions")
        scale_mm = float(reference.get("depth_value_scale_mm") or 1.0)
        return values.reshape(height, width).astype(np.float32) * (scale_mm / 1000.0)

    @staticmethod
    def _reference_timestamp(reference: dict[str, Any]) -> int:
        # Use one timestamp domain consistently for video and IMU. Some SDK
        # builds expose global timestamps for video but not for IMU, which made
        # the former per-sample fallback mix clock domains during startup.
        return int(
            reference.get("system_timestamp_us")
            or reference.get("global_timestamp_us")
            or reference.get("device_timestamp_us")
            or 0
        )

    @staticmethod
    def _sample_timestamp(sample: Any) -> int:
        return int(
            sample.system_timestamp_us
            or sample.global_timestamp_us
            or sample.device_timestamp_us
        )

    def _require_reader(self) -> CameraSharedMemory:
        if self.reader is None:
            raise RuntimeError("camera shared memory is not open")
        return self.reader

    def _close_reader(self) -> None:
        if self.reader is not None:
            self.reader.close()
            self.reader = None
        self.mapping_name = None

    def _observation(
        self,
        *,
        stream: str,
        schema: str,
        sequence: int,
        observed_at_us: int,
        data: Any,
        freshness_ms: Optional[int],
        coordinate_frame: Optional[str] = None,
        calibration_revision: Optional[str] = None,
    ) -> dict[str, Any]:
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
            "coordinate_frame": coordinate_frame,
            "calibration_revision": calibration_revision,
            "clock_domain": "camera_system_timestamp_preferred",
            "valid": True,
            "data": data,
        }

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
        capability_ready = self.ready and self.residency == "HOT"
        return {
            "provider_id": self.provider_id,
            "instance_id": self.instance_id,
            "boot_id": self.boot_id,
            "residency": self.residency,
            "health": self.health,
            "ready": self.ready,
            "pid": os.getpid(),
            "details": {
                "provider_version": "0.2.2",
                "backend": self.args.backend,
                "backend_classification": "INERTIAL_FIRST_ERROR_STATE_FILTER_WITH_RGBD_VISUAL_UPDATES_AND_OPTIONAL_IR_FALLBACK",
                "world_up_axis": [0.0, 1.0, 0.0],
                "world_down_axis": [0.0, -1.0, 0.0],
                "gravity_stabilization": {
                    "correction_applied": self.backend.last_gravity_correction_applied,
                    "tilt_error_rad": self.backend.last_gravity_tilt_error_rad,
                    "tracking_sample_count": self.backend.last_gravity_tracking_sample_count,
                    "direction_std_rad": self.backend.last_gravity_direction_std_rad,
                    "stationary_duration_s": self.backend.last_gravity_stationary_duration_s,
                    "mode": self.backend.last_gravity_correction_mode,
                    "adjustment_state": self.backend.last_gravity_adjustment_state,
                    "gyro_configured_limit_radps": self.backend.gravity_gyro_limit_radps,
                    "gyro_effective_limit_radps": self.backend.gravity_gyro_effective_limit_radps,
                    "gyro_noise_floor_radps": self.backend.gravity_gyro_noise_floor_radps,
                    "gyro_rms_radps": self.backend.last_gravity_gyro_rms_radps,
                    "gyro_p95_radps": self.backend.last_gravity_gyro_p95_radps,
                    "gyro_bias_imu_radps": self.backend.gyro_bias_imu_radps.tolist(),
                    "tracking_rotation_leveling_enabled": True,
                    "tracking_translation_mutation_enabled": False,
                },
                "feature_tracking": {
                    "configured_mode": self.backend.rgb_frontend.feature_preprocess_mode,
                    "selected_mode": self.backend.last_feature_preprocess_mode,
                    "raw_keypoint_count": self.backend.last_raw_keypoint_count,
                    "normalized_keypoint_count": self.backend.last_normalized_keypoint_count,
                    "frame_luma_median": self.backend.last_frame_luma_median,
                    "raw_keypoint_trigger": self.backend.rgb_frontend.lcn_raw_keypoint_trigger,
                    "raw_inlier_accept": self.backend.rgb_frontend.lcn_raw_inlier_accept,
                    "ir_enabled": bool(getattr(self.args, "ir_enabled", True)),
                    "ir_keypoint_count": self.backend.last_ir_keypoint_count,
                    "ir_inlier_count": self.backend.last_ir_inlier_count,
                },
                "production_backend_target": "openvins_or_basalt_native_adapter",
                "inertial_filter": {
                    "state": "orientation_position_velocity_gyro_bias_accel_bias",
                    "visual_measurements": "rgbd_metric_pose_with_optional_ir_depth_fallback",
                    "high_rate_prediction_hz": float(getattr(self.args, "inertial_publish_hz", 100.0)),
                    "last_pose_update_mode": getattr(self.backend, "last_pose_update_mode", "UNKNOWN"),
                    "last_visual_sensor": getattr(self.backend, "last_visual_sensor", "NONE"),
                    "last_visual_update_accepted": getattr(self.backend, "last_visual_update_accepted", False),
                },
                "session_epoch": self.session_epoch,
                "world_frame": self.world_frame,
                "tracking_state": self.last_tracking_state,
                "last_error": self.last_error,
                "manager_error": self.manager_error,
                "motion_inhibited": self.motion_inhibited,
                "imu_gap_count": self.imu_gap_count,
                "capability_readiness": {
                    "localization.vio.local_pose": capability_ready,
                    "localization.vio.body_pose": capability_ready,
                    "localization.vio.velocity": capability_ready,
                    "localization.vio.tracking_status": self.residency == "HOT",
                    "localization.vio.dynamic_transform": capability_ready,
                    "localization.vio.reset": self.residency == "HOT",
                },
                "resource_profile": {
                    "basis": "ESTIMATED",
                    "ram_mb": 350,
                    "vram_mb": "NOT_APPLICABLE",
                    "cpu_cores_expected": 2.5,
                },
            },
        }


class ControlHandler(BaseHTTPRequestHandler):
    provider: LocalVioProvider

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
            elif self.path == "/v1/control/stop":
                result = self.provider.stop()
            elif self.path == "/v1/control/request":
                request = self._read_json()
                result = self.provider.handle_request(request)
            else:
                self._reply(404, {"error": "not found"})
                return
            self._reply(200, result)
        except Exception as error:
            self.provider.health = "UNHEALTHY"
            self.provider.last_error = str(error)
            self._reply(500, {"error": str(error)})

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        payload = self.rfile.read(length) if length > 0 else b"{}"
        value = json.loads(payload.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("request body must be a JSON object")
        return value

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[LocalVioControl] {format % args}", flush=True)

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
    parser.add_argument("--control-port", type=int, default=7102)
    parser.add_argument(
        "--backend",
        choices=("inertial_first_rgbd_eskf",),
        default="inertial_first_rgbd_eskf",
    )
    parser.add_argument("--poll-interval", type=float, default=0.005)
    parser.add_argument("--gravity-samples", type=int, default=80)
    parser.add_argument("--gravity-std-limit-mps2", type=float, default=0.35)
    parser.add_argument("--gravity-gyro-limit-radps", type=float, default=0.012)
    parser.add_argument("--inertial-publish-hz", type=float, default=100.0)
    parser.add_argument("--ir-sync-tolerance-us", type=int, default=8000)
    parser.add_argument("--ir-enabled", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument(
        "--feature-preprocess-mode",
        choices=("raw_baseline", "adaptive_circular_lcn"),
        default="adaptive_circular_lcn",
    )
    parser.add_argument("--lcn-radius-px", type=int, default=11)
    parser.add_argument("--lcn-low-light-median", type=float, default=105.0)
    parser.add_argument("--lcn-low-contrast-span", type=float, default=70.0)
    parser.add_argument("--lcn-raw-keypoint-trigger", type=int, default=700)
    parser.add_argument("--lcn-raw-inlier-accept", type=int, default=70)
    parser.add_argument("--lcn-selection-margin", type=float, default=0.12)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    provider = LocalVioProvider(args)
    ControlHandler.provider = provider
    server = ThreadingHTTPServer(("127.0.0.1", args.control_port), ControlHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request_stop(_signum: int, _frame: Any) -> None:
        provider.stop()

    if os.name != "nt":
        signal.signal(signal.SIGTERM, request_stop)
    try:
        return provider.run()
    except KeyboardInterrupt:
        provider.stop()
        return 130
    except Exception as error:
        provider.health = "UNHEALTHY"
        provider.last_error = str(error)
        print(f"[LocalVioProvider] fatal: {error}", file=sys.stderr, flush=True)
        return 1
    finally:
        server.shutdown()
        server.server_close()
        provider._close_reader()


if __name__ == "__main__":
    raise SystemExit(main())
