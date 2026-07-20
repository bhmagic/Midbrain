from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Any, Iterable

import cv2
import numpy as np

from .math3d import gravity_aligned_world_from_camera, invert_transform, make_transform, rotation_angle
from .prototype_backend import (
    PoseResult,
    PrototypeRgbdImuOdometry,
    STANDARD_GRAVITY_MPS2,
    WORLD_UP,
    _FeatureSet,
    _FrameState,
    _axis_angle_rotation,
    _gravity_target_rotation,
    _rotation_axis_angle,
)

GRAVITY_WORLD = np.array([0.0, -STANDARD_GRAVITY_MPS2, 0.0], dtype=np.float64)


@dataclass
class _FilterState:
    timestamp_us: int
    rotation_world_from_imu: np.ndarray
    position_world_m: np.ndarray
    velocity_world_mps: np.ndarray
    gyro_bias_imu_radps: np.ndarray
    accel_bias_imu_mps2: np.ndarray
    covariance: np.ndarray

    def copy(self) -> "_FilterState":
        return _FilterState(
            timestamp_us=int(self.timestamp_us),
            rotation_world_from_imu=self.rotation_world_from_imu.copy(),
            position_world_m=self.position_world_m.copy(),
            velocity_world_mps=self.velocity_world_mps.copy(),
            gyro_bias_imu_radps=self.gyro_bias_imu_radps.copy(),
            accel_bias_imu_mps2=self.accel_bias_imu_mps2.copy(),
            covariance=self.covariance.copy(),
        )


@dataclass
class _VisualTrack:
    state: _FrameState
    world_from_sensor_anchor: np.ndarray


@dataclass
class _VisualCandidate:
    sensor: str
    world_from_imu_measurement: np.ndarray
    relative_current_from_previous: np.ndarray
    inlier_count: int
    match_count: int
    feature_mode: str
    solver_mode: str
    reprojection_rmse_px: float
    raw_keypoint_count: int
    normalized_keypoint_count: int
    luma_median: float
    position_innovation_m: float
    rotation_innovation_rad: float
    normalized_score: float


class InertialFirstRgbdVio:
    """Inertial-first RGB-D/IR visual-inertial estimator.

    The committed navigation state is propagated from every ordered IMU sample.
    RGB-D and optional IR/depth observations are metric pose measurements that
    correct the inertial error state; they are not the primary motion clock.

    The filter is a 15-state multiplicative error-state filter:
      orientation, position, velocity, gyro bias, accelerometer bias.

    It intentionally keeps the existing quiet-IMU gravity-leveling behavior as a
    bounded roll/pitch reference. Translation remains owned by inertial dynamics
    plus visual measurement updates.
    """

    def __init__(
        self,
        *,
        max_features: int = 2200,
        min_inliers: int = 28,
        gravity_samples: int = 80,
        gravity_std_limit_mps2: float = 0.35,
        gravity_tracking_window_s: float = 1.0,
        gravity_tracking_min_samples: int = 20,
        gravity_magnitude_tolerance_mps2: float = 1.2,
        gravity_direction_std_limit_rad: float = 0.12,
        gravity_gyro_limit_radps: float = 0.012,
        gravity_tracking_delay_s: float = 0.35,
        gravity_tracking_gain: float = 0.018,
        gravity_tracking_max_step_rad: float = 0.0015,
        gravity_recovery_delay_s: float = 0.6,
        gravity_recovery_gain: float = 0.22,
        gravity_recovery_max_step_rad: float = 0.025,
        max_visual_position_innovation_m: float = 1.2,
        max_visual_rotation_innovation_rad: float = 1.3,
        visual_mahalanobis_limit: float = 45.0,
        visual_stale_timeout_s: float = 0.45,
        imu_max_step_s: float = 0.01,
        gyro_noise_density_radps_sqrt_hz: float = 0.008,
        accel_noise_density_mps2_sqrt_hz: float = 0.08,
        gyro_bias_random_walk_radps2_sqrt_hz: float = 0.0002,
        accel_bias_random_walk_mps3_sqrt_hz: float = 0.002,
        feature_preprocess_mode: str = "adaptive_circular_lcn",
        lcn_radius_px: int = 11,
        lcn_low_light_median: float = 105.0,
        lcn_low_contrast_span: float = 70.0,
        lcn_raw_keypoint_trigger: int = 700,
        lcn_raw_inlier_accept: int = 70,
        lcn_selection_margin: float = 0.12,
        ir_enabled: bool = True,
        ir_rgb_inlier_margin: int = 8,
    ) -> None:
        self.gravity_samples_required = int(gravity_samples)
        self.gravity_std_limit_mps2 = float(gravity_std_limit_mps2)
        self.gravity_tracking_window_us = int(max(0.2, gravity_tracking_window_s) * 1_000_000)
        self.gravity_tracking_min_samples = max(6, int(gravity_tracking_min_samples))
        self.gravity_magnitude_tolerance_mps2 = max(0.1, float(gravity_magnitude_tolerance_mps2))
        self.gravity_direction_std_limit_rad = max(0.01, float(gravity_direction_std_limit_rad))
        self.gravity_gyro_limit_radps = max(0.002, float(gravity_gyro_limit_radps))
        self.gravity_gyro_effective_limit_radps = self.gravity_gyro_limit_radps
        self.gravity_gyro_noise_floor_radps: float | None = None
        self.gravity_tracking_delay_s = max(0.1, float(gravity_tracking_delay_s))
        self.gravity_tracking_gain = float(np.clip(gravity_tracking_gain, 0.0, 0.2))
        self.gravity_tracking_max_step_rad = max(0.0001, float(gravity_tracking_max_step_rad))
        self.gravity_recovery_delay_s = max(0.2, float(gravity_recovery_delay_s))
        self.gravity_recovery_gain = float(np.clip(gravity_recovery_gain, 0.0, 1.0))
        self.gravity_recovery_max_step_rad = max(0.001, float(gravity_recovery_max_step_rad))
        self.max_visual_position_innovation_m = max(0.1, float(max_visual_position_innovation_m))
        self.max_visual_rotation_innovation_rad = max(0.1, float(max_visual_rotation_innovation_rad))
        self.visual_mahalanobis_limit = max(6.0, float(visual_mahalanobis_limit))
        self.visual_stale_timeout_s = max(0.1, float(visual_stale_timeout_s))
        self.imu_max_step_s = float(np.clip(imu_max_step_s, 0.001, 0.05))
        self.ir_enabled = bool(ir_enabled)
        self.ir_rgb_inlier_margin = max(0, int(ir_rgb_inlier_margin))

        self.gyro_noise_density = max(1e-6, float(gyro_noise_density_radps_sqrt_hz))
        self.accel_noise_density = max(1e-6, float(accel_noise_density_mps2_sqrt_hz))
        self.gyro_bias_random_walk = max(1e-8, float(gyro_bias_random_walk_radps2_sqrt_hz))
        self.accel_bias_random_walk = max(1e-8, float(accel_bias_random_walk_mps3_sqrt_hz))

        frontend_args = dict(
            max_features=max_features,
            min_inliers=min_inliers,
            gravity_samples=gravity_samples,
            gravity_std_limit_mps2=gravity_std_limit_mps2,
            gravity_gyro_limit_radps=gravity_gyro_limit_radps,
            feature_preprocess_mode=feature_preprocess_mode,
            lcn_radius_px=lcn_radius_px,
            lcn_low_light_median=lcn_low_light_median,
            lcn_low_contrast_span=lcn_low_contrast_span,
            lcn_raw_keypoint_trigger=lcn_raw_keypoint_trigger,
            lcn_raw_inlier_accept=lcn_raw_inlier_accept,
            lcn_selection_margin=lcn_selection_margin,
        )
        self.rgb_frontend = PrototypeRgbdImuOdometry(**frontend_args)
        self.ir_frontend = PrototypeRgbdImuOdometry(**frontend_args)
        self.min_inliers = int(min_inliers)

        self.camera_matrix: np.ndarray | None = None
        self.ir_camera_matrix: np.ndarray | None = None
        self.color_from_imu = np.eye(4, dtype=np.float64)
        self.imu_from_color = np.eye(4, dtype=np.float64)
        self.color_from_ir = np.eye(4, dtype=np.float64)
        self.ir_from_color = np.eye(4, dtype=np.float64)
        self.state: _FilterState | None = None
        self.previous_rgb: _VisualTrack | None = None
        self.previous_ir: _VisualTrack | None = None
        self.last_visual_update_us: int | None = None
        self.last_visual_sensor = "NONE"
        self.last_visual_reprojection_rmse_px: float | None = None
        self.last_visual_correction_position_m = 0.0
        self.last_visual_correction_rotation_rad = 0.0
        self.last_visual_update_accepted = False
        self.last_pose_update_mode = "INITIALIZING"
        self.last_propagation_steps = 0
        self.last_result_timestamp_us = 0

        self.acceleration_samples: deque[np.ndarray] = deque(maxlen=max(400, gravity_samples * 3))
        self.acceleration_history: deque[tuple[int, np.ndarray]] = deque(maxlen=12000)
        self.gyro_samples: deque[tuple[int, np.ndarray]] = deque(maxlen=12000)
        self.gravity_std_mps2: float | None = None
        self.gyro_bias_imu_radps = np.zeros(3, dtype=np.float64)
        self.gravity_stationary_since_us: int | None = None
        self.initialized = False

        self.last_gravity_tracking_sample_count = 0
        self.last_gravity_correction_applied = False
        self.last_gravity_tilt_error_rad: float | None = None
        self.last_gravity_direction_std_rad: float | None = None
        self.last_gravity_stationary_duration_s = 0.0
        self.last_gravity_correction_mode = "UNAVAILABLE"
        self.last_gravity_adjustment_state = "OFF"
        self.last_gravity_gyro_rms_radps: float | None = None
        self.last_gravity_gyro_p95_radps: float | None = None

        self.last_rotation_source = "NONE"
        self.last_rotation_disagreement_rad: float | None = None
        self.last_gyro_rotation_sample_count = 0
        self.last_gyro_rotation_angle_rad: float | None = None
        self.last_feature_preprocess_mode = "RAW_BASELINE"
        self.last_raw_keypoint_count = 0
        self.last_normalized_keypoint_count = 0
        self.last_frame_luma_median = 0.0
        self.last_ir_keypoint_count = 0
        self.last_ir_inlier_count = 0
        self.last_ir_frame_luma_median = 0.0
        self.last_initialization_blocker: str | None = "WAITING_FOR_IMU"
        self.last_initialization_accelerometer_window_count = 0
        self.last_initialization_gyroscope_window_count = 0
        self.last_initialization_accelerometer_rate_hz: float | None = None
        self.last_initialization_gyroscope_rate_hz: float | None = None

    def configure(
        self,
        camera_matrix: np.ndarray,
        color_from_imu: np.ndarray | None,
        *,
        ir_camera_matrix: np.ndarray | None = None,
        color_from_ir: np.ndarray | None = None,
        imu_noise: dict[str, float] | None = None,
    ) -> None:
        matrix = np.asarray(camera_matrix, dtype=np.float64)
        if matrix.shape != (3, 3):
            raise ValueError("camera_matrix must be 3x3")
        self.camera_matrix = matrix
        self.rgb_frontend.configure(matrix, color_from_imu)

        if color_from_imu is not None:
            transform = np.asarray(color_from_imu, dtype=np.float64)
            if transform.shape != (4, 4):
                raise ValueError("color_from_imu must be 4x4")
            self.color_from_imu = transform
            self.imu_from_color = invert_transform(transform)

        if ir_camera_matrix is not None:
            ir_matrix = np.asarray(ir_camera_matrix, dtype=np.float64)
            if ir_matrix.shape != (3, 3):
                raise ValueError("ir_camera_matrix must be 3x3")
            self.ir_camera_matrix = ir_matrix
            self.ir_frontend.configure(ir_matrix, None)

        if color_from_ir is not None:
            transform = np.asarray(color_from_ir, dtype=np.float64)
            if transform.shape != (4, 4):
                raise ValueError("color_from_ir must be 4x4")
            self.color_from_ir = transform
            self.ir_from_color = invert_transform(transform)

        if imu_noise:
            self.gyro_noise_density = max(
                1e-6,
                float(imu_noise.get("gyro_noise_density", self.gyro_noise_density)),
            )
            self.accel_noise_density = max(
                1e-6,
                float(imu_noise.get("accel_noise_density", self.accel_noise_density)),
            )
            self.gyro_bias_random_walk = max(
                1e-8,
                float(imu_noise.get("gyro_random_walk", self.gyro_bias_random_walk)),
            )
            self.accel_bias_random_walk = max(
                1e-8,
                float(imu_noise.get("accel_random_walk", self.accel_bias_random_walk)),
            )

    def reset(self) -> None:
        self.state = None
        self.previous_rgb = None
        self.previous_ir = None
        self.last_visual_update_us = None
        self.last_visual_sensor = "NONE"
        self.last_visual_reprojection_rmse_px = None
        self.last_visual_correction_position_m = 0.0
        self.last_visual_correction_rotation_rad = 0.0
        self.last_visual_update_accepted = False
        self.last_pose_update_mode = "INITIALIZING"
        self.last_propagation_steps = 0
        self.last_result_timestamp_us = 0
        self.acceleration_samples.clear()
        self.acceleration_history.clear()
        self.gyro_samples.clear()
        self.gravity_std_mps2 = None
        self.gyro_bias_imu_radps = np.zeros(3, dtype=np.float64)
        self.gravity_gyro_effective_limit_radps = self.gravity_gyro_limit_radps
        self.gravity_gyro_noise_floor_radps = None
        self.gravity_stationary_since_us = None
        self.initialized = False
        self.last_gravity_tracking_sample_count = 0
        self.last_gravity_correction_applied = False
        self.last_gravity_tilt_error_rad = None
        self.last_gravity_direction_std_rad = None
        self.last_gravity_stationary_duration_s = 0.0
        self.last_gravity_correction_mode = "UNAVAILABLE"
        self.last_gravity_adjustment_state = "OFF"
        self.last_gravity_gyro_rms_radps = None
        self.last_gravity_gyro_p95_radps = None
        self.last_rotation_source = "NONE"
        self.last_rotation_disagreement_rad = None
        self.last_gyro_rotation_sample_count = 0
        self.last_gyro_rotation_angle_rad = None
        self.last_feature_preprocess_mode = "RAW_BASELINE"
        self.last_raw_keypoint_count = 0
        self.last_normalized_keypoint_count = 0
        self.last_frame_luma_median = 0.0
        self.last_ir_keypoint_count = 0
        self.last_ir_inlier_count = 0
        self.last_ir_frame_luma_median = 0.0
        self.last_initialization_blocker = "WAITING_FOR_IMU"
        self.last_initialization_accelerometer_window_count = 0
        self.last_initialization_gyroscope_window_count = 0
        self.last_initialization_accelerometer_rate_hz = None
        self.last_initialization_gyroscope_rate_hz = None

    def add_accelerometer(
        self,
        timestamp_us: int,
        value_imu_mps2: np.ndarray,
        *,
        motion_inhibited: bool,
    ) -> None:
        value = np.asarray(value_imu_mps2, dtype=np.float64)
        if value.shape != (3,) or not np.all(np.isfinite(value)):
            return
        timestamp = int(timestamp_us)
        self.acceleration_history.append((timestamp, value.copy()))
        if motion_inhibited and not self.initialized:
            self.acceleration_samples.append(value.copy())

    def add_gyroscope(self, timestamp_us: int, value_imu_radps: np.ndarray) -> None:
        value = np.asarray(value_imu_radps, dtype=np.float64)
        if value.shape != (3,) or not np.all(np.isfinite(value)):
            return
        self.gyro_samples.append((int(timestamp_us), value.copy()))

    def process(
        self,
        rgb: np.ndarray,
        depth_m: np.ndarray,
        timestamp_us: int,
        *,
        ir_gray: np.ndarray | None = None,
        ir_depth_m: np.ndarray | None = None,
        ir_timestamp_us: int | None = None,
    ) -> PoseResult:
        if self.camera_matrix is None:
            raise RuntimeError("camera calibration is not configured")
        timestamp = int(timestamp_us)
        if not self.initialized:
            if not self._try_initialize(timestamp):
                self.last_pose_update_mode = "INITIALIZING"
                self.last_rotation_source = "INITIALIZING"
                return self._result(
                    timestamp,
                    tracking_state="INITIALIZING",
                    message="waiting for stationary accelerometer and gyroscope samples while motion is inhibited",
                )

        assert self.state is not None
        self.state, propagation_steps = self._propagate_state(self.state, timestamp)
        self.last_propagation_steps = propagation_steps
        predicted_world_from_camera = self._world_from_camera(self.state)

        rgb_gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        rgb_current = self._make_frame(self.rgb_frontend, rgb_gray, depth_m, timestamp)
        self.last_frame_luma_median = rgb_current.luma_median
        self.last_raw_keypoint_count = len(rgb_current.features["RAW_BASELINE"].keypoints)
        self.last_normalized_keypoint_count = len(
            rgb_current.features.get("CIRCULAR_LCN", _FeatureSet("CIRCULAR_LCN", [], None)).keypoints
        )

        first_visual_keyframe = self.previous_rgb is None
        rgb_candidate = self._visual_candidate(
            sensor="RGBD",
            frontend=self.rgb_frontend,
            previous_track=self.previous_rgb,
            current=rgb_current,
            predicted_world_from_sensor=predicted_world_from_camera,
            sensor_to_color=np.eye(4, dtype=np.float64),
        )

        ir_candidate: _VisualCandidate | None = None
        ir_current: _FrameState | None = None
        if (
            self.ir_enabled
            and ir_gray is not None
            and ir_depth_m is not None
            and ir_timestamp_us is not None
            and self.ir_camera_matrix is not None
        ):
            ir_timestamp = int(ir_timestamp_us)
            ir_image = np.asarray(ir_gray, dtype=np.uint8)
            ir_current = self._make_frame(self.ir_frontend, ir_image, ir_depth_m, ir_timestamp)
            self.last_ir_frame_luma_median = ir_current.luma_median
            self.last_ir_keypoint_count = len(ir_current.features["RAW_BASELINE"].keypoints)
            predicted_world_from_ir = predicted_world_from_camera @ self.color_from_ir
            ir_candidate = self._visual_candidate(
                sensor="IR_DEPTH",
                frontend=self.ir_frontend,
                previous_track=self.previous_ir,
                current=ir_current,
                predicted_world_from_sensor=predicted_world_from_ir,
                sensor_to_color=self.ir_from_color,
            )
            self.last_ir_inlier_count = ir_candidate.inlier_count if ir_candidate else 0
        else:
            self.last_ir_keypoint_count = 0
            self.last_ir_inlier_count = 0
            self.last_ir_frame_luma_median = 0.0

        selected = self._select_visual_candidate(rgb_candidate, ir_candidate)
        visual_accepted = False
        message: str | None = None
        if selected is not None:
            visual_accepted = self._apply_visual_measurement(selected)
            if visual_accepted:
                self.last_visual_update_us = timestamp
                self.last_visual_sensor = selected.sensor
                self.last_visual_reprojection_rmse_px = selected.reprojection_rmse_px
                self.last_feature_preprocess_mode = selected.feature_mode
                self.last_rotation_source = f"IMU_PROPAGATION_PLUS_{selected.solver_mode}"
                self.last_rotation_disagreement_rad = selected.rotation_innovation_rad
                message = (
                    f"{selected.sensor} corrected inertial state; "
                    f"{selected.inlier_count} inliers, RMSE {selected.reprojection_rmse_px:.2f}px"
                )
            else:
                message = (
                    f"{selected.sensor} measurement rejected by inertial innovation gate; "
                    "continuing IMU propagation"
                )
        else:
            self.last_rotation_source = "IMU_PROPAGATION"
            self.last_rotation_disagreement_rad = None
            message = "visual update unavailable; continuing IMU propagation"

        # The visual anchor for the next relative measurement is always the fused
        # pose at this frame. This allows reacquisition after a failed frame without
        # letting a rejected visual transform corrupt the navigation state.
        fused_world_from_camera = self._world_from_camera(self.state)
        self.previous_rgb = _VisualTrack(rgb_current, fused_world_from_camera.copy())
        if ir_current is not None:
            self.previous_ir = _VisualTrack(
                ir_current,
                fused_world_from_camera @ self.color_from_ir,
            )

        if first_visual_keyframe:
            self.last_visual_update_us = timestamp
            self.last_visual_sensor = "RGBD"
            self.last_rotation_source = "IMU_PROPAGATION_FIRST_VISUAL_KEYFRAME"
            self.last_visual_update_accepted = True
            visual_accepted = True
            message = "first visual keyframe anchored to the inertial state"
        else:
            self.last_visual_update_accepted = visual_accepted

        self._apply_gravity_reference(
            correction_policy="TRACKING_STABILIZE" if visual_accepted else "DEGRADED_RECOVERY"
        )

        stale_s = self._visual_stale_seconds(timestamp)
        if first_visual_keyframe:
            tracking_state = "TRACKING"
            self.last_pose_update_mode = "IMU_PROPAGATION_FIRST_VISUAL_KEYFRAME"
        elif visual_accepted:
            tracking_state = "TRACKING"
            self.last_pose_update_mode = "IMU_PROPAGATION_WITH_VISUAL_CORRECTION"
        elif stale_s is not None and stale_s <= self.visual_stale_timeout_s:
            tracking_state = "TRACKING"
            self.last_pose_update_mode = "IMU_PROPAGATION_BETWEEN_VISUAL_UPDATES"
        else:
            tracking_state = "DEGRADED"
            self.last_pose_update_mode = "IMU_PROPAGATION_VISUAL_STALE"

        current_world_from_camera = self._world_from_camera(self.state)
        translation_step = float(
            np.linalg.norm(current_world_from_camera[:3, 3] - predicted_world_from_camera[:3, 3])
        )
        rotation_step = rotation_angle(
            predicted_world_from_camera[:3, :3].T @ current_world_from_camera[:3, :3]
        )
        return self._result(
            timestamp,
            tracking_state=tracking_state,
            inlier_count=selected.inlier_count if selected else 0,
            match_count=selected.match_count if selected else 0,
            translation_step_m=translation_step,
            rotation_step_rad=rotation_step,
            message=message,
        )

    def predict_latest(self) -> PoseResult | None:
        """Return a non-committing high-rate inertial prediction.

        This mirrors the fast-propagate pattern used by mature VIO systems: the
        committed filter remains at the last camera update, while consumers can
        receive a pose predicted to the newest common IMU timestamp.
        """
        if not self.initialized or self.state is None:
            return None
        latest = self._latest_common_imu_timestamp()
        if latest is None or latest <= self.state.timestamp_us:
            return None
        predicted, steps = self._propagate_state(self.state.copy(), latest)
        previous_state = self.state
        self.state = predicted
        try:
            stale_s = self._visual_stale_seconds(latest)
            tracking_state = (
                "TRACKING"
                if stale_s is not None and stale_s <= self.visual_stale_timeout_s
                else "DEGRADED"
            )
            old_mode = self.last_pose_update_mode
            old_rotation_source = self.last_rotation_source
            old_visual_accepted = self.last_visual_update_accepted
            self.last_pose_update_mode = "IMU_FAST_PROPAGATION"
            self.last_rotation_source = "IMU_FAST_PROPAGATION"
            self.last_visual_update_accepted = False
            self.last_propagation_steps = steps
            result = self._result(
                latest,
                tracking_state=tracking_state,
                message="high-rate inertial prediction between camera updates",
            )
            self.last_pose_update_mode = old_mode
            self.last_rotation_source = old_rotation_source
            self.last_visual_update_accepted = old_visual_accepted
            return result
        finally:
            self.state = previous_state

    def _try_initialize(self, camera_timestamp_us: int) -> bool:
        # Initialization is an IMU operation. Camera and IMU timestamps can have
        # a fixed offset or can expose different "best available" SDK fields, so
        # never select the stationary gyro window around an RGB timestamp. Use the
        # newest timestamp shared by accelerometer and gyroscope histories.
        if len(self.acceleration_samples) < self.gravity_samples_required:
            self.last_initialization_blocker = "WAITING_FOR_ACCELEROMETER_SAMPLES"
            return False
        imu_timestamp_us = self._latest_common_imu_timestamp()
        if imu_timestamp_us is None:
            self.last_initialization_blocker = "WAITING_FOR_ACCELEROMETER_AND_GYROSCOPE_HISTORY"
            return False

        initialization_sample_count = max(self.gravity_samples_required, 20)
        recent_acceleration, accel_span_us = self._tail_window_by_count(
            self.acceleration_history,
            imu_timestamp_us,
            initialization_sample_count,
            max_span_us=5_000_000,
        )
        self.last_initialization_accelerometer_window_count = len(recent_acceleration)
        self.last_initialization_accelerometer_rate_hz = self._sample_rate_hz(
            len(recent_acceleration), accel_span_us
        )
        if len(recent_acceleration) < initialization_sample_count:
            self.last_initialization_blocker = "WAITING_FOR_RECENT_ACCELEROMETER_WINDOW"
            return False
        samples = np.stack(recent_acceleration, axis=0)
        magnitudes = np.linalg.norm(samples, axis=1)
        self.gravity_std_mps2 = float(np.std(magnitudes))
        if self.gravity_std_mps2 > self.gravity_std_limit_mps2:
            self.last_initialization_blocker = "ACCELEROMETER_NOT_STATIONARY"
            return False

        gyro_window, gyro_span_us = self._tail_window_by_count(
            self.gyro_samples,
            imu_timestamp_us,
            initialization_sample_count,
            max_span_us=5_000_000,
        )
        self.last_initialization_gyroscope_window_count = len(gyro_window)
        self.last_initialization_gyroscope_rate_hz = self._sample_rate_hz(
            len(gyro_window), gyro_span_us
        )
        if len(gyro_window) < initialization_sample_count:
            self.last_initialization_blocker = "WAITING_FOR_RECENT_GYROSCOPE_WINDOW"
            return False
        gyro_array = np.stack(gyro_window, axis=0)
        gyro_bias = np.median(gyro_array, axis=0)
        gyro_residual_norms = np.linalg.norm(gyro_array - gyro_bias, axis=1)
        median_residual = float(np.median(gyro_residual_norms))
        mad = float(np.median(np.abs(gyro_residual_norms - median_residual)))
        noise_ceiling = median_residual + 4.0 * 1.4826 * mad
        effective_limit = float(
            np.clip(max(self.gravity_gyro_limit_radps, 1.5 * noise_ceiling), 0.008, 0.03)
        )
        gyro_p95 = float(np.percentile(gyro_residual_norms, 95.0))
        self.last_gravity_gyro_rms_radps = float(
            np.sqrt(np.mean(np.square(gyro_residual_norms)))
        )
        self.last_gravity_gyro_p95_radps = gyro_p95
        self.gravity_gyro_noise_floor_radps = noise_ceiling
        self.gravity_gyro_effective_limit_radps = effective_limit
        if gyro_p95 > effective_limit:
            self.last_initialization_blocker = "GYROSCOPE_NOT_STATIONARY"
            return False

        mean_accel_imu = np.mean(samples, axis=0)
        mean_accel_color = self.color_from_imu[:3, :3] @ mean_accel_imu
        rotation_world_from_color = gravity_aligned_world_from_camera(mean_accel_color)
        world_from_color = make_transform(rotation_world_from_color, [0.0, 0.0, 0.0])
        world_from_imu = world_from_color @ self.color_from_imu

        accel_direction = mean_accel_imu / max(1e-12, float(np.linalg.norm(mean_accel_imu)))
        accel_bias = mean_accel_imu - accel_direction * STANDARD_GRAVITY_MPS2

        covariance = np.diag(
            np.square(
                np.array(
                    [
                        0.03,
                        0.03,
                        0.08,
                        0.05,
                        0.05,
                        0.05,
                        0.10,
                        0.10,
                        0.10,
                        0.01,
                        0.01,
                        0.01,
                        0.08,
                        0.08,
                        0.08,
                    ],
                    dtype=np.float64,
                )
            )
        )
        self.state = _FilterState(
            timestamp_us=int(imu_timestamp_us),
            rotation_world_from_imu=world_from_imu[:3, :3].copy(),
            position_world_m=world_from_imu[:3, 3].copy(),
            velocity_world_mps=np.zeros(3, dtype=np.float64),
            gyro_bias_imu_radps=gyro_bias.copy(),
            accel_bias_imu_mps2=accel_bias.copy(),
            covariance=covariance,
        )
        self.gyro_bias_imu_radps = gyro_bias.copy()
        self._estimate_stationary_gyro_baseline()
        self.last_initialization_blocker = None
        self.initialized = True
        self.last_pose_update_mode = "IMU_INITIALIZED"
        self.last_rotation_source = "IMU_INITIALIZATION"
        return True

    def _make_frame(
        self,
        frontend: PrototypeRgbdImuOdometry,
        gray: np.ndarray,
        depth_m: np.ndarray,
        timestamp_us: int,
    ) -> _FrameState:
        image = np.asarray(gray, dtype=np.uint8)
        return _FrameState(
            timestamp_us=int(timestamp_us),
            gray=image,
            depth_m=np.asarray(depth_m, dtype=np.float32),
            features=frontend._extract_feature_sets(image),
            luma_median=float(np.median(image)),
        )

    def _visual_candidate(
        self,
        *,
        sensor: str,
        frontend: PrototypeRgbdImuOdometry,
        previous_track: _VisualTrack | None,
        current: _FrameState,
        predicted_world_from_sensor: np.ndarray,
        sensor_to_color: np.ndarray,
    ) -> _VisualCandidate | None:
        if previous_track is None:
            return None
        predicted_current_from_previous = (
            invert_transform(predicted_world_from_sensor)
            @ previous_track.world_from_sensor_anchor
        )
        result = frontend._estimate_best_step(
            previous_track.state,
            current,
            gyro_current_from_previous=predicted_current_from_previous[:3, :3],
        )
        if result is None:
            return None
        (
            current_from_previous,
            inlier_count,
            match_count,
            feature_mode,
            solver_mode,
            reprojection_rmse_px,
        ) = result
        world_from_sensor_measurement = (
            previous_track.world_from_sensor_anchor @ invert_transform(current_from_previous)
        )
        if sensor == "RGBD":
            world_from_color_measurement = world_from_sensor_measurement
        else:
            world_from_color_measurement = world_from_sensor_measurement @ sensor_to_color
        world_from_imu_measurement = world_from_color_measurement @ self.color_from_imu

        assert self.state is not None
        rotation_innovation = rotation_angle(
            self.state.rotation_world_from_imu.T
            @ world_from_imu_measurement[:3, :3]
        )
        position_innovation = float(
            np.linalg.norm(world_from_imu_measurement[:3, 3] - self.state.position_world_m)
        )
        quality = inlier_count / max(1.0, 1.0 + reprojection_rmse_px)
        penalty = 16.0 * rotation_innovation + 5.0 * position_innovation
        normalized_score = float(quality - penalty)
        raw_count = len(current.features["RAW_BASELINE"].keypoints)
        normalized_count = len(
            current.features.get("CIRCULAR_LCN", _FeatureSet("CIRCULAR_LCN", [], None)).keypoints
        )
        return _VisualCandidate(
            sensor=sensor,
            world_from_imu_measurement=world_from_imu_measurement,
            relative_current_from_previous=current_from_previous,
            inlier_count=int(inlier_count),
            match_count=int(match_count),
            feature_mode=str(feature_mode),
            solver_mode=str(solver_mode),
            reprojection_rmse_px=float(reprojection_rmse_px),
            raw_keypoint_count=raw_count,
            normalized_keypoint_count=normalized_count,
            luma_median=current.luma_median,
            position_innovation_m=position_innovation,
            rotation_innovation_rad=rotation_innovation,
            normalized_score=normalized_score,
        )

    def _select_visual_candidate(
        self,
        rgb: _VisualCandidate | None,
        ir: _VisualCandidate | None,
    ) -> _VisualCandidate | None:
        if rgb is None:
            return ir
        if ir is None:
            return rgb
        # Preserve the proven RGB-D baseline unless IR is materially stronger or
        # RGB is already weak. IR is a low-light resilience measurement, not a
        # replacement for healthy color tracking.
        rgb_healthy = (
            rgb.inlier_count >= 70
            and rgb.reprojection_rmse_px <= 2.5
            and rgb.rotation_innovation_rad <= 0.35
        )
        if rgb_healthy:
            return rgb
        if (
            ir.inlier_count >= rgb.inlier_count + self.ir_rgb_inlier_margin
            and ir.normalized_score > rgb.normalized_score
        ):
            return ir
        return rgb

    def _apply_visual_measurement(self, candidate: _VisualCandidate) -> bool:
        assert self.state is not None
        if candidate.position_innovation_m > self.max_visual_position_innovation_m:
            return False
        if candidate.rotation_innovation_rad > self.max_visual_rotation_innovation_rad:
            return False

        rotation_measurement = candidate.world_from_imu_measurement[:3, :3]
        position_measurement = candidate.world_from_imu_measurement[:3, 3]
        residual_rotation = _so3_log(
            self.state.rotation_world_from_imu.T @ rotation_measurement
        )
        residual_position = position_measurement - self.state.position_world_m
        residual = np.concatenate((residual_rotation, residual_position))

        inliers = max(self.min_inliers, candidate.inlier_count)
        rmse = max(0.2, candidate.reprojection_rmse_px)
        rotation_sigma = float(np.clip(0.008 + 0.28 / math.sqrt(inliers) + 0.004 * rmse, 0.008, 0.12))
        position_sigma = float(np.clip(0.010 + 0.45 / math.sqrt(inliers) + 0.006 * rmse, 0.010, 0.18))
        measurement_covariance = np.diag(
            [rotation_sigma**2] * 3 + [position_sigma**2] * 3
        )
        measurement_matrix = np.zeros((6, 15), dtype=np.float64)
        measurement_matrix[0:3, 0:3] = np.eye(3)
        measurement_matrix[3:6, 3:6] = np.eye(3)
        innovation_covariance = (
            measurement_matrix @ self.state.covariance @ measurement_matrix.T
            + measurement_covariance
        )
        try:
            innovation_inverse = np.linalg.inv(innovation_covariance)
        except np.linalg.LinAlgError:
            return False
        mahalanobis = float(residual.T @ innovation_inverse @ residual)
        if not math.isfinite(mahalanobis) or mahalanobis > self.visual_mahalanobis_limit:
            return False

        kalman_gain = (
            self.state.covariance
            @ measurement_matrix.T
            @ innovation_inverse
        )
        correction = kalman_gain @ residual
        self._apply_error_state(self.state, correction)
        identity = np.eye(15, dtype=np.float64)
        left = identity - kalman_gain @ measurement_matrix
        self.state.covariance = (
            left @ self.state.covariance @ left.T
            + kalman_gain @ measurement_covariance @ kalman_gain.T
        )
        self.state.covariance = _symmetrize(self.state.covariance)
        self.gyro_bias_imu_radps = self.state.gyro_bias_imu_radps.copy()
        self.last_visual_correction_rotation_rad = float(np.linalg.norm(correction[0:3]))
        self.last_visual_correction_position_m = float(np.linalg.norm(correction[3:6]))
        return True

    def _propagate_state(
        self,
        initial: _FilterState,
        target_timestamp_us: int,
    ) -> tuple[_FilterState, int]:
        target = int(target_timestamp_us)
        if target <= initial.timestamp_us:
            return initial.copy(), 0
        if not self.acceleration_history or not self.gyro_samples:
            result = initial.copy()
            result.timestamp_us = target
            return result, 0

        breakpoints = {initial.timestamp_us, target}
        for timestamp, _ in self.acceleration_history:
            if initial.timestamp_us < timestamp < target:
                breakpoints.add(int(timestamp))
        for timestamp, _ in self.gyro_samples:
            if initial.timestamp_us < timestamp < target:
                breakpoints.add(int(timestamp))
        ordered = sorted(breakpoints)
        state = initial.copy()
        steps = 0
        for left_us, right_us in zip(ordered, ordered[1:]):
            interval_s = (right_us - left_us) / 1_000_000.0
            if interval_s <= 0.0:
                continue
            subdivisions = max(1, int(math.ceil(interval_s / self.imu_max_step_s)))
            for index in range(subdivisions):
                sub_left = left_us + (right_us - left_us) * index / subdivisions
                sub_right = left_us + (right_us - left_us) * (index + 1) / subdivisions
                midpoint = int(round(0.5 * (sub_left + sub_right)))
                accel = _interpolate_history(self.acceleration_history, midpoint)
                gyro = _interpolate_history(self.gyro_samples, midpoint)
                if accel is None or gyro is None:
                    continue
                dt = (sub_right - sub_left) / 1_000_000.0
                self._propagate_one(state, accel, gyro, dt)
                steps += 1
            state.timestamp_us = int(right_us)
        state.timestamp_us = target
        return state, steps

    def _propagate_one(
        self,
        state: _FilterState,
        acceleration_imu_mps2: np.ndarray,
        gyroscope_imu_radps: np.ndarray,
        dt: float,
    ) -> None:
        if dt <= 0.0:
            return
        omega = np.asarray(gyroscope_imu_radps, dtype=np.float64) - state.gyro_bias_imu_radps
        specific_force = np.asarray(acceleration_imu_mps2, dtype=np.float64) - state.accel_bias_imu_mps2
        delta_rotation = _so3_exp(omega * dt)
        rotation_mid = state.rotation_world_from_imu @ _so3_exp(omega * (0.5 * dt))
        acceleration_world = rotation_mid @ specific_force + GRAVITY_WORLD
        state.position_world_m = (
            state.position_world_m
            + state.velocity_world_mps * dt
            + 0.5 * acceleration_world * dt * dt
        )
        state.velocity_world_mps = state.velocity_world_mps + acceleration_world * dt
        state.rotation_world_from_imu = _orthonormalize(
            state.rotation_world_from_imu @ delta_rotation
        )

        transition_rate = np.zeros((15, 15), dtype=np.float64)
        transition_rate[0:3, 0:3] = -_skew(omega)
        transition_rate[0:3, 9:12] = -np.eye(3)
        transition_rate[3:6, 6:9] = np.eye(3)
        transition_rate[6:9, 0:3] = -rotation_mid @ _skew(specific_force)
        transition_rate[6:9, 12:15] = -rotation_mid
        transition = np.eye(15, dtype=np.float64) + transition_rate * dt

        noise_map = np.zeros((15, 12), dtype=np.float64)
        noise_map[0:3, 0:3] = -np.eye(3)
        noise_map[6:9, 3:6] = -rotation_mid
        noise_map[9:12, 6:9] = np.eye(3)
        noise_map[12:15, 9:12] = np.eye(3)
        continuous_noise = np.diag(
            [self.gyro_noise_density**2] * 3
            + [self.accel_noise_density**2] * 3
            + [self.gyro_bias_random_walk**2] * 3
            + [self.accel_bias_random_walk**2] * 3
        )
        discrete_noise = noise_map @ continuous_noise @ noise_map.T * dt
        state.covariance = transition @ state.covariance @ transition.T + discrete_noise
        state.covariance = _symmetrize(state.covariance)

    @staticmethod
    def _apply_error_state(state: _FilterState, correction: np.ndarray) -> None:
        correction = np.asarray(correction, dtype=np.float64).reshape(15)
        state.rotation_world_from_imu = _orthonormalize(
            state.rotation_world_from_imu @ _so3_exp(correction[0:3])
        )
        state.position_world_m += correction[3:6]
        state.velocity_world_mps += correction[6:9]
        state.gyro_bias_imu_radps += correction[9:12]
        state.accel_bias_imu_mps2 += correction[12:15]

    def _apply_gravity_reference(self, *, correction_policy: str) -> None:
        self.last_gravity_correction_applied = False
        self.last_gravity_adjustment_state = "OFF"
        observation = self._stationary_gravity_observation()
        if observation is None or self.state is None:
            self.gravity_stationary_since_us = None
            self.last_gravity_tracking_sample_count = 0
            self.last_gravity_stationary_duration_s = 0.0
            self.last_gravity_correction_mode = "GYRO_OR_ACCEL_NOT_STABLE"
            return

        mean_imu, sample_count, direction_std, window_start_us, window_end_us = observation
        if self.gravity_stationary_since_us is None:
            self.gravity_stationary_since_us = window_start_us
        stationary_duration_s = max(
            0.0,
            (window_end_us - self.gravity_stationary_since_us) / 1_000_000.0,
        )
        world_from_camera = self._world_from_camera(self.state)
        mean_color = self.color_from_imu[:3, :3] @ mean_imu
        norm = float(np.linalg.norm(mean_color))
        if norm <= 1e-9:
            self.last_gravity_correction_mode = "INVALID_GRAVITY"
            return
        up_camera = mean_color / norm
        observed_up_world = world_from_camera[:3, :3] @ up_camera
        observed_up_world /= max(1e-12, float(np.linalg.norm(observed_up_world)))
        tilt_error = math.acos(float(np.clip(observed_up_world @ WORLD_UP, -1.0, 1.0)))
        self.last_gravity_tracking_sample_count = sample_count
        self.last_gravity_tilt_error_rad = tilt_error
        self.last_gravity_direction_std_rad = direction_std
        self.last_gravity_stationary_duration_s = stationary_duration_s

        if correction_policy == "TRACKING_STABILIZE":
            delay_s = self.gravity_tracking_delay_s
            gain = self.gravity_tracking_gain
            max_step = self.gravity_tracking_max_step_rad
            active_mode = "TRACKING_LEVELING_ACTIVE"
        else:
            delay_s = self.gravity_recovery_delay_s
            gain = self.gravity_recovery_gain
            max_step = self.gravity_recovery_max_step_rad
            active_mode = "DEGRADED_LEVELING_ACTIVE"

        self.last_gravity_adjustment_state = "READY"
        if stationary_duration_s < delay_s:
            self.last_gravity_correction_mode = "WAITING_FOR_STABLE_GYRO"
            return
        if tilt_error <= math.radians(0.08):
            self.last_gravity_correction_mode = "GRAVITY_ALIGNED"
            return

        target_world_from_camera_rotation = _gravity_target_rotation(
            world_from_camera[:3, :3],
            up_camera,
        )
        correction_world = (
            target_world_from_camera_rotation @ world_from_camera[:3, :3].T
        )
        axis, total_angle = _rotation_axis_angle(correction_world)
        if axis is None or total_angle <= 1e-8:
            self.last_gravity_correction_mode = "GRAVITY_ALIGNED"
            return
        correction_angle = min(max_step, total_angle * gain)
        if correction_angle <= 1e-8:
            self.last_gravity_correction_mode = "GRAVITY_READY"
            return
        correction_rotation = _axis_angle_rotation(axis, correction_angle)
        self.state.rotation_world_from_imu = _orthonormalize(
            correction_rotation @ self.state.rotation_world_from_imu
        )
        self.state.covariance[0:3, 0:3] *= 0.98
        self.last_gravity_correction_applied = True
        self.last_gravity_adjustment_state = "ACTIVE"
        self.last_gravity_correction_mode = active_mode

        # Quiet windows are also the safest time to refine gyro zero-rate bias.
        gyro_values = self._values_between(
            self.gyro_samples,
            window_start_us,
            window_end_us,
        )
        if gyro_values:
            observed_bias = np.median(np.stack(gyro_values, axis=0), axis=0)
            self.state.gyro_bias_imu_radps = (
                0.995 * self.state.gyro_bias_imu_radps + 0.005 * observed_bias
            )
            self.gyro_bias_imu_radps = self.state.gyro_bias_imu_radps.copy()

    def _estimate_stationary_gyro_baseline(self) -> None:
        if len(self.gyro_samples) < 10:
            return
        latest_us = int(self.gyro_samples[-1][0])
        values = self._values_before(self.gyro_samples, latest_us, max(self.gravity_tracking_window_us, 1_000_000))
        if len(values) < 10:
            return
        array = np.stack(values, axis=0)
        bias = np.median(array, axis=0)
        residual_norms = np.linalg.norm(array - bias, axis=1)
        median_residual = float(np.median(residual_norms))
        mad = float(np.median(np.abs(residual_norms - median_residual)))
        noise_ceiling = median_residual + 4.0 * 1.4826 * mad
        self.gyro_bias_imu_radps = bias
        self.gravity_gyro_noise_floor_radps = noise_ceiling
        self.gravity_gyro_effective_limit_radps = float(
            np.clip(max(self.gravity_gyro_limit_radps, 1.5 * noise_ceiling), 0.008, 0.03)
        )

    def _stationary_gravity_observation(
        self,
    ) -> tuple[np.ndarray, int, float, int, int] | None:
        if not self.acceleration_history or not self.gyro_samples:
            return None
        window_end_us = min(
            int(self.acceleration_history[-1][0]),
            int(self.gyro_samples[-1][0]),
        )
        window_start_us = window_end_us - self.gravity_tracking_window_us
        acceleration = self._values_between(
            self.acceleration_history,
            window_start_us,
            window_end_us,
        )
        if len(acceleration) < self.gravity_tracking_min_samples:
            return None
        samples = np.stack(acceleration, axis=0)
        magnitudes = np.linalg.norm(samples, axis=1)
        mean_magnitude = float(np.mean(magnitudes))
        magnitude_std = float(np.std(magnitudes))
        if abs(mean_magnitude - STANDARD_GRAVITY_MPS2) > self.gravity_magnitude_tolerance_mps2:
            return None
        if magnitude_std > self.gravity_std_limit_mps2:
            return None
        normalized = samples / np.maximum(magnitudes[:, None], 1e-9)
        mean_direction = np.mean(normalized, axis=0)
        mean_direction /= max(1e-12, float(np.linalg.norm(mean_direction)))
        angles = np.arccos(np.clip(normalized @ mean_direction, -1.0, 1.0))
        direction_std = float(np.sqrt(np.mean(np.square(angles))))
        if direction_std > self.gravity_direction_std_limit_rad:
            return None

        gyroscope = self._values_between(self.gyro_samples, window_start_us, window_end_us)
        if len(gyroscope) < 2:
            return None
        gyro_array = np.stack(gyroscope, axis=0)
        bias = self.state.gyro_bias_imu_radps if self.state is not None else self.gyro_bias_imu_radps
        residual_norms = np.linalg.norm(gyro_array - bias, axis=1)
        self.last_gravity_gyro_rms_radps = float(np.sqrt(np.mean(np.square(residual_norms))))
        self.last_gravity_gyro_p95_radps = float(np.percentile(residual_norms, 95.0))
        if self.last_gravity_gyro_p95_radps > self.gravity_gyro_effective_limit_radps:
            return None
        return (
            np.mean(samples, axis=0),
            len(acceleration),
            direction_std,
            window_start_us,
            window_end_us,
        )

    def _world_from_camera(self, state: _FilterState) -> np.ndarray:
        world_from_imu = make_transform(
            state.rotation_world_from_imu,
            state.position_world_m,
        )
        return world_from_imu @ self.imu_from_color

    def _visual_stale_seconds(self, timestamp_us: int) -> float | None:
        if self.last_visual_update_us is None:
            return None
        return max(0.0, (int(timestamp_us) - self.last_visual_update_us) / 1_000_000.0)

    def _latest_common_imu_timestamp(self) -> int | None:
        if not self.acceleration_history or not self.gyro_samples:
            return None
        return min(int(self.acceleration_history[-1][0]), int(self.gyro_samples[-1][0]))

    @staticmethod
    def _tail_window_by_count(
        history: Iterable[tuple[int, np.ndarray]],
        end_us: int,
        sample_count: int,
        *,
        max_span_us: int,
    ) -> tuple[list[np.ndarray], int | None]:
        """Return the newest fixed-count sample window ending at or before end_us.

        Initialization must be independent of the configured IMU rate. A fixed
        1.5-second window cannot contain 80 samples at 50 Hz, so select by count
        and only reject histories whose required samples are implausibly stale.
        """
        eligible = [
            (int(timestamp), value)
            for timestamp, value in history
            if int(timestamp) <= int(end_us)
        ]
        if len(eligible) < int(sample_count):
            return [value for _, value in eligible], None
        selected = eligible[-int(sample_count):]
        span_us = max(0, selected[-1][0] - selected[0][0])
        if span_us > int(max_span_us):
            return [], span_us
        return [value for _, value in selected], span_us

    @staticmethod
    def _sample_rate_hz(sample_count: int, span_us: int | None) -> float | None:
        if span_us is None or span_us <= 0 or sample_count < 2:
            return None
        return float((sample_count - 1) * 1_000_000.0 / span_us)

    @staticmethod
    def _values_before(
        history: Iterable[tuple[int, np.ndarray]],
        end_us: int,
        duration_us: int,
    ) -> list[np.ndarray]:
        start_us = int(end_us) - int(duration_us)
        return [value for timestamp, value in history if start_us <= timestamp <= end_us]

    @staticmethod
    def _values_between(
        history: Iterable[tuple[int, np.ndarray]],
        start_us: int,
        end_us: int,
    ) -> list[np.ndarray]:
        return [value for timestamp, value in history if start_us <= timestamp <= end_us]

    def _result(
        self,
        timestamp_us: int,
        *,
        tracking_state: str,
        inlier_count: int = 0,
        match_count: int = 0,
        translation_step_m: float = 0.0,
        rotation_step_rad: float = 0.0,
        message: str | None,
    ) -> PoseResult:
        if self.state is None:
            world_from_camera = np.eye(4, dtype=np.float64)
            velocity = np.zeros(3, dtype=np.float64)
            gyro_bias = np.zeros(3, dtype=np.float64)
            accel_bias = np.zeros(3, dtype=np.float64)
            position_std = np.zeros(3, dtype=np.float64)
            rotation_std = np.zeros(3, dtype=np.float64)
            covariance_6x6: tuple[float, ...] = ()
            state_timestamp = None
        else:
            world_from_camera = self._world_from_camera(self.state)
            velocity = self.state.velocity_world_mps.copy()
            gyro_bias = self.state.gyro_bias_imu_radps.copy()
            accel_bias = self.state.accel_bias_imu_mps2.copy()
            rotation_std = np.sqrt(np.maximum(0.0, np.diag(self.state.covariance)[0:3]))
            position_std = np.sqrt(np.maximum(0.0, np.diag(self.state.covariance)[3:6]))
            covariance_6x6 = tuple(self._pose_covariance_6x6(self.state).reshape(-1).tolist())
            state_timestamp = int(self.state.timestamp_us)
        stale_s = self._visual_stale_seconds(timestamp_us)
        return PoseResult(
            timestamp_us=int(timestamp_us),
            world_from_camera=world_from_camera,
            velocity_world_mps=velocity,
            tracking_state=tracking_state,
            inlier_count=int(inlier_count),
            match_count=int(match_count),
            translation_step_m=float(translation_step_m),
            rotation_step_rad=float(rotation_step_rad),
            gravity_sample_count=len(self.acceleration_samples),
            gravity_std_mps2=self.gravity_std_mps2,
            gyro_delta_rad=self.last_gyro_rotation_angle_rad,
            gravity_tracking_sample_count=self.last_gravity_tracking_sample_count,
            gravity_correction_applied=self.last_gravity_correction_applied,
            gravity_tilt_error_rad=self.last_gravity_tilt_error_rad,
            gravity_direction_std_rad=self.last_gravity_direction_std_rad,
            gravity_stationary_duration_s=self.last_gravity_stationary_duration_s,
            gravity_correction_mode=self.last_gravity_correction_mode,
            gravity_adjustment_state=self.last_gravity_adjustment_state,
            gravity_gyro_rms_radps=self.last_gravity_gyro_rms_radps,
            gravity_gyro_p95_radps=self.last_gravity_gyro_p95_radps,
            gravity_gyro_noise_floor_radps=self.gravity_gyro_noise_floor_radps,
            gravity_gyro_effective_limit_radps=self.gravity_gyro_effective_limit_radps,
            rotation_source=self.last_rotation_source,
            rotation_disagreement_rad=self.last_rotation_disagreement_rad,
            gyro_rotation_sample_count=self.last_gyro_rotation_sample_count,
            gyro_rotation_angle_rad=self.last_gyro_rotation_angle_rad,
            feature_preprocess_mode=self.last_feature_preprocess_mode,
            raw_keypoint_count=self.last_raw_keypoint_count,
            normalized_keypoint_count=self.last_normalized_keypoint_count,
            frame_luma_median=self.last_frame_luma_median,
            message=message,
            pose_update_mode=self.last_pose_update_mode,
            visual_update_accepted=self.last_visual_update_accepted,
            visual_sensor=self.last_visual_sensor,
            visual_reprojection_rmse_px=self.last_visual_reprojection_rmse_px,
            visual_correction_position_m=self.last_visual_correction_position_m,
            visual_correction_rotation_rad=self.last_visual_correction_rotation_rad,
            visual_stale_s=stale_s,
            imu_propagation_steps=self.last_propagation_steps,
            imu_state_timestamp_us=state_timestamp,
            estimated_gyro_bias_radps=tuple(float(value) for value in gyro_bias),
            estimated_accel_bias_mps2=tuple(float(value) for value in accel_bias),
            filter_position_std_m=tuple(float(value) for value in position_std),
            filter_rotation_std_rad=tuple(float(value) for value in rotation_std),
            covariance_6x6=covariance_6x6,
            ir_keypoint_count=self.last_ir_keypoint_count,
            ir_inlier_count=self.last_ir_inlier_count,
            ir_frame_luma_median=self.last_ir_frame_luma_median,
            imu_accelerometer_history_count=len(self.acceleration_history),
            imu_gyroscope_history_count=len(self.gyro_samples),
            imu_timestamp_skew_us=(
                int(self.acceleration_history[-1][0]) - int(self.gyro_samples[-1][0])
                if self.acceleration_history and self.gyro_samples
                else None
            ),
            initialization_blocker=self.last_initialization_blocker,
            initialization_accelerometer_window_count=(
                self.last_initialization_accelerometer_window_count
            ),
            initialization_gyroscope_window_count=(
                self.last_initialization_gyroscope_window_count
            ),
            initialization_accelerometer_rate_hz=(
                self.last_initialization_accelerometer_rate_hz
            ),
            initialization_gyroscope_rate_hz=(
                self.last_initialization_gyroscope_rate_hz
            ),
        )

    @staticmethod
    def _pose_covariance_6x6(state: _FilterState) -> np.ndarray:
        covariance = np.zeros((6, 6), dtype=np.float64)
        # Fabric pose covariance convention remains position then rotation.
        covariance[0:3, 0:3] = state.covariance[3:6, 3:6]
        covariance[0:3, 3:6] = state.covariance[3:6, 0:3]
        covariance[3:6, 0:3] = state.covariance[0:3, 3:6]
        covariance[3:6, 3:6] = state.covariance[0:3, 0:3]
        return covariance


def _skew(vector: np.ndarray) -> np.ndarray:
    x, y, z = np.asarray(vector, dtype=np.float64).reshape(3)
    return np.array(
        [[0.0, -z, y], [z, 0.0, -x], [-y, x, 0.0]],
        dtype=np.float64,
    )


def _so3_exp(vector: np.ndarray) -> np.ndarray:
    value = np.asarray(vector, dtype=np.float64).reshape(3)
    angle = float(np.linalg.norm(value))
    if angle < 1e-10:
        return np.eye(3, dtype=np.float64) + _skew(value)
    axis = value / angle
    skew = _skew(axis)
    return np.eye(3, dtype=np.float64) + math.sin(angle) * skew + (1.0 - math.cos(angle)) * (skew @ skew)


def _so3_log(rotation: np.ndarray) -> np.ndarray:
    matrix = _orthonormalize(np.asarray(rotation, dtype=np.float64).reshape(3, 3))
    angle = rotation_angle(matrix)
    if angle < 1e-10:
        return np.array(
            [
                0.5 * (matrix[2, 1] - matrix[1, 2]),
                0.5 * (matrix[0, 2] - matrix[2, 0]),
                0.5 * (matrix[1, 0] - matrix[0, 1]),
            ],
            dtype=np.float64,
        )
    denominator = 2.0 * math.sin(angle)
    axis = np.array(
        [
            matrix[2, 1] - matrix[1, 2],
            matrix[0, 2] - matrix[2, 0],
            matrix[1, 0] - matrix[0, 1],
        ],
        dtype=np.float64,
    ) / denominator
    return axis * angle


def _orthonormalize(rotation: np.ndarray) -> np.ndarray:
    u, _, vh = np.linalg.svd(np.asarray(rotation, dtype=np.float64).reshape(3, 3))
    result = u @ vh
    if np.linalg.det(result) < 0.0:
        u[:, -1] *= -1.0
        result = u @ vh
    return result


def _interpolate_history(
    history: Iterable[tuple[int, np.ndarray]],
    timestamp_us: int,
) -> np.ndarray | None:
    samples = list(history)
    if not samples:
        return None
    target = int(timestamp_us)
    if target <= samples[0][0]:
        return samples[0][1].copy()
    if target >= samples[-1][0]:
        return samples[-1][1].copy()
    previous_time, previous_value = samples[0]
    for current_time, current_value in samples[1:]:
        if current_time >= target:
            if current_time == previous_time:
                return current_value.copy()
            alpha = (target - previous_time) / (current_time - previous_time)
            return previous_value + (current_value - previous_value) * alpha
        previous_time, previous_value = current_time, current_value
    return samples[-1][1].copy()


def _symmetrize(matrix: np.ndarray) -> np.ndarray:
    value = 0.5 * (matrix + matrix.T)
    diagonal = np.maximum(np.diag(value), 1e-12)
    value[np.diag_indices_from(value)] = diagonal
    return value
