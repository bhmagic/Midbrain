from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np

from .math3d import (
    gravity_aligned_world_from_camera,
    invert_transform,
    make_transform,
    rotation_angle,
)

STANDARD_GRAVITY_MPS2 = 9.80665
WORLD_UP = np.array([0.0, 0.0, 1.0], dtype=np.float64)


@dataclass(frozen=True)
class PoseResult:
    timestamp_us: int
    world_from_camera: np.ndarray
    velocity_world_mps: np.ndarray
    tracking_state: str
    inlier_count: int
    match_count: int
    translation_step_m: float
    rotation_step_rad: float
    gravity_sample_count: int
    gravity_std_mps2: float | None
    gyro_delta_rad: float | None
    gravity_tracking_sample_count: int
    gravity_correction_applied: bool
    gravity_tilt_error_rad: float | None
    gravity_direction_std_rad: float | None
    gravity_stationary_duration_s: float
    gravity_correction_mode: str
    gravity_adjustment_state: str
    gravity_gyro_rms_radps: float | None
    gravity_gyro_p95_radps: float | None
    gravity_gyro_noise_floor_radps: float | None
    gravity_gyro_effective_limit_radps: float
    rotation_source: str
    rotation_disagreement_rad: float | None
    gyro_rotation_sample_count: int
    gyro_rotation_angle_rad: float | None
    feature_preprocess_mode: str
    raw_keypoint_count: int
    normalized_keypoint_count: int
    frame_luma_median: float
    message: str | None
    pose_update_mode: str = "UNKNOWN"
    visual_update_accepted: bool = False
    visual_sensor: str = "NONE"
    visual_reprojection_rmse_px: float | None = None
    visual_correction_position_m: float = 0.0
    visual_correction_rotation_rad: float = 0.0
    visual_stale_s: float | None = None
    imu_propagation_steps: int = 0
    imu_state_timestamp_us: int | None = None
    estimated_gyro_bias_radps: tuple[float, float, float] = (0.0, 0.0, 0.0)
    estimated_accel_bias_mps2: tuple[float, float, float] = (0.0, 0.0, 0.0)
    filter_position_std_m: tuple[float, float, float] = (0.0, 0.0, 0.0)
    filter_rotation_std_rad: tuple[float, float, float] = (0.0, 0.0, 0.0)
    covariance_6x6: tuple[float, ...] = ()
    ir_keypoint_count: int = 0
    ir_inlier_count: int = 0
    ir_frame_luma_median: float = 0.0
    imu_accelerometer_history_count: int = 0
    imu_gyroscope_history_count: int = 0
    imu_timestamp_skew_us: int | None = None
    initialization_blocker: str | None = None
    initialization_accelerometer_window_count: int = 0
    initialization_gyroscope_window_count: int = 0
    initialization_accelerometer_rate_hz: float | None = None
    initialization_gyroscope_rate_hz: float | None = None


@dataclass(frozen=True)
class _FeatureSet:
    name: str
    keypoints: list[Any]
    descriptors: np.ndarray | None


@dataclass
class _FrameState:
    timestamp_us: int
    gray: np.ndarray
    depth_m: np.ndarray
    features: dict[str, _FeatureSet]
    luma_median: float


class PrototypeRgbdImuOdometry:
    """Experimental metric RGB-D odometry with gravity stabilization.

    This remains a prototype rather than a tightly coupled production VIO. RGB-D
    visual odometry provides the metric pose step. The accelerometer establishes
    the initial gravity direction and continuously monitors roll/pitch drift.
    Gravity acts as a slow complementary roll/pitch reference whenever the IMU is quiet.
    Visual odometry still owns translation and fast rotation; gravity preserves yaw and
    changes rotation only, with a much stronger correction reserved for degraded tracking.
    """

    def __init__(
        self,
        *,
        max_features: int = 2200,
        min_inliers: int = 28,
        gravity_samples: int = 80,
        gravity_std_limit_mps2: float = 0.35,
        max_translation_step_m: float = 0.8,
        max_rotation_step_rad: float = 1.2,
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
        gyro_visual_disagreement_base_rad: float = 0.18,
        gyro_visual_disagreement_scale: float = 0.30,
        gyro_seed_min_angle_rad: float = 0.04,
        feature_preprocess_mode: str = "adaptive_circular_lcn",
        lcn_radius_px: int = 11,
        lcn_low_light_median: float = 105.0,
        lcn_low_contrast_span: float = 70.0,
        lcn_raw_keypoint_trigger: int = 700,
        lcn_raw_inlier_accept: int = 70,
        lcn_selection_margin: float = 0.12,
    ):
        self.orb = cv2.ORB_create(nfeatures=max_features, fastThreshold=12)
        self.matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)
        self.min_inliers = min_inliers
        self.gravity_samples_required = gravity_samples
        self.gravity_std_limit_mps2 = gravity_std_limit_mps2
        self.max_translation_step_m = max_translation_step_m
        self.max_rotation_step_rad = max_rotation_step_rad
        self.gravity_tracking_window_us = int(max(0.2, gravity_tracking_window_s) * 1_000_000)
        self.gravity_tracking_min_samples = max(6, gravity_tracking_min_samples)
        self.gravity_magnitude_tolerance_mps2 = max(0.1, gravity_magnitude_tolerance_mps2)
        self.gravity_direction_std_limit_rad = max(0.01, gravity_direction_std_limit_rad)
        self.gravity_gyro_limit_radps = max(0.002, gravity_gyro_limit_radps)
        self.gravity_gyro_effective_limit_radps = self.gravity_gyro_limit_radps
        self.gravity_gyro_noise_floor_radps: float | None = None
        self.gyro_bias_imu_radps = np.zeros(3, dtype=np.float64)
        self.gravity_tracking_delay_s = max(0.1, gravity_tracking_delay_s)
        self.gravity_tracking_gain = min(0.2, max(0.0, gravity_tracking_gain))
        self.gravity_tracking_max_step_rad = max(0.0001, gravity_tracking_max_step_rad)
        self.gravity_recovery_delay_s = max(0.2, gravity_recovery_delay_s)
        self.gravity_recovery_gain = min(1.0, max(0.0, gravity_recovery_gain))
        self.gravity_recovery_max_step_rad = max(0.001, gravity_recovery_max_step_rad)
        self.gyro_visual_disagreement_base_rad = max(0.05, gyro_visual_disagreement_base_rad)
        self.gyro_visual_disagreement_scale = max(0.0, gyro_visual_disagreement_scale)
        self.gyro_seed_min_angle_rad = max(0.0, gyro_seed_min_angle_rad)
        if feature_preprocess_mode not in {"raw_baseline", "adaptive_circular_lcn"}:
            raise ValueError("feature_preprocess_mode must be raw_baseline or adaptive_circular_lcn")
        self.feature_preprocess_mode = feature_preprocess_mode
        self.lcn_radius_px = max(2, int(lcn_radius_px))
        self.lcn_low_light_median = float(lcn_low_light_median)
        self.lcn_low_contrast_span = max(1.0, float(lcn_low_contrast_span))
        self.lcn_raw_keypoint_trigger = max(self.min_inliers, int(lcn_raw_keypoint_trigger))
        self.lcn_raw_inlier_accept = max(self.min_inliers, int(lcn_raw_inlier_accept))
        self.lcn_selection_margin = max(0.0, float(lcn_selection_margin))
        self._lcn_kernel = _circular_kernel(self.lcn_radius_px)
        self.camera_matrix: np.ndarray | None = None
        self.color_from_imu = np.eye(4, dtype=np.float64)
        self.world_from_camera = np.eye(4, dtype=np.float64)
        self.previous: _FrameState | None = None
        self.last_velocity = np.zeros(3, dtype=np.float64)
        self.acceleration_samples: deque[np.ndarray] = deque(maxlen=max(400, gravity_samples * 3))
        self.acceleration_history: deque[tuple[int, np.ndarray]] = deque(maxlen=4000)
        self.gyro_samples: deque[tuple[int, np.ndarray]] = deque(maxlen=4000)
        self.gravity_std_mps2: float | None = None
        self.last_gravity_tracking_sample_count = 0
        self.last_gravity_correction_applied = False
        self.last_gravity_tilt_error_rad: float | None = None
        self.last_gravity_direction_std_rad: float | None = None
        self.last_gravity_stationary_duration_s = 0.0
        self.last_gravity_correction_mode = "UNAVAILABLE"
        self.last_gravity_adjustment_state = "OFF"
        self.last_feature_preprocess_mode = "RAW_BASELINE"
        self.last_raw_keypoint_count = 0
        self.last_normalized_keypoint_count = 0
        self.last_frame_luma_median = 0.0
        self.last_gravity_gyro_rms_radps: float | None = None
        self.last_gravity_gyro_p95_radps: float | None = None
        self.last_rotation_source = "NONE"
        self.last_rotation_disagreement_rad: float | None = None
        self.last_gyro_rotation_sample_count = 0
        self.last_gyro_rotation_angle_rad: float | None = None
        self.gravity_stationary_since_us: int | None = None
        self.initialized = False

    def configure(self, camera_matrix: np.ndarray, color_from_imu: np.ndarray | None) -> None:
        matrix = np.asarray(camera_matrix, dtype=np.float64)
        if matrix.shape != (3, 3):
            raise ValueError("camera_matrix must be 3x3")
        self.camera_matrix = matrix
        if color_from_imu is not None:
            transform = np.asarray(color_from_imu, dtype=np.float64)
            if transform.shape != (4, 4):
                raise ValueError("color_from_imu must be 4x4")
            self.color_from_imu = transform

    def reset(self) -> None:
        self.world_from_camera = np.eye(4, dtype=np.float64)
        self.previous = None
        self.last_velocity = np.zeros(3, dtype=np.float64)
        self.acceleration_samples.clear()
        self.acceleration_history.clear()
        self.gyro_samples.clear()
        self.gravity_std_mps2 = None
        self.last_gravity_tracking_sample_count = 0
        self.last_gravity_correction_applied = False
        self.last_gravity_tilt_error_rad = None
        self.last_gravity_direction_std_rad = None
        self.last_gravity_stationary_duration_s = 0.0
        self.last_gravity_correction_mode = "UNAVAILABLE"
        self.last_gravity_adjustment_state = "OFF"
        self.last_feature_preprocess_mode = "RAW_BASELINE"
        self.last_raw_keypoint_count = 0
        self.last_normalized_keypoint_count = 0
        self.last_frame_luma_median = 0.0
        self.last_gravity_gyro_rms_radps = None
        self.last_gravity_gyro_p95_radps = None
        self.gravity_gyro_effective_limit_radps = self.gravity_gyro_limit_radps
        self.gravity_gyro_noise_floor_radps = None
        self.gyro_bias_imu_radps = np.zeros(3, dtype=np.float64)
        self.last_rotation_source = "NONE"
        self.last_rotation_disagreement_rad = None
        self.last_gyro_rotation_sample_count = 0
        self.last_gyro_rotation_angle_rad = None
        self.gravity_stationary_since_us = None
        self.initialized = False

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
        self.acceleration_history.append((timestamp, value))
        if motion_inhibited and not self.initialized:
            self.acceleration_samples.append(value)

    def add_gyroscope(self, timestamp_us: int, value_imu_radps: np.ndarray) -> None:
        value = np.asarray(value_imu_radps, dtype=np.float64)
        if value.shape == (3,) and np.all(np.isfinite(value)):
            self.gyro_samples.append((int(timestamp_us), value))

    def process(self, rgb: np.ndarray, depth_m: np.ndarray, timestamp_us: int) -> PoseResult:
        if self.camera_matrix is None:
            raise RuntimeError("camera calibration is not configured")
        if not self.initialized:
            initialized = self._try_initialize_gravity()
            if not initialized:
                self.last_rotation_source = "INITIALIZING"
                return self._status_result(
                    timestamp_us,
                    "INITIALIZING",
                    message="waiting for stationary accelerometer and gyroscope samples while motion is inhibited",
                )

        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        current = _FrameState(
            timestamp_us=timestamp_us,
            gray=gray,
            depth_m=depth_m,
            features=self._extract_feature_sets(gray),
            luma_median=float(np.median(gray)),
        )
        self.last_frame_luma_median = current.luma_median
        self.last_raw_keypoint_count = len(current.features["RAW_BASELINE"].keypoints)
        self.last_normalized_keypoint_count = len(
            current.features.get("CIRCULAR_LCN", _FeatureSet("CIRCULAR_LCN", [], None)).keypoints
        )
        self.last_feature_preprocess_mode = (
            "RAW_AND_CIRCULAR_LCN_CANDIDATES"
            if "CIRCULAR_LCN" in current.features
            else "RAW_BASELINE"
        )

        if self.previous is None:
            self.previous = current
            self.last_feature_preprocess_mode = "RAW_BASELINE"
            self.last_rotation_source = "FIRST_VISUAL_KEYFRAME"
            self.last_rotation_disagreement_rad = None
            self.last_gyro_rotation_sample_count = 0
            self.last_gyro_rotation_angle_rad = None
            self._update_gravity_reference(correction_policy="TRACKING_STABILIZE")
            return self._status_result(timestamp_us, "TRACKING", message="first visual keyframe")

        gyro_previous_from_current, gyro_sample_count = self._integrated_gyro_rotation(
            self.previous.timestamp_us,
            timestamp_us,
        )
        gyro_current_from_previous = (
            gyro_previous_from_current.T if gyro_previous_from_current is not None else None
        )
        gyro_angle = (
            rotation_angle(gyro_previous_from_current)
            if gyro_previous_from_current is not None
            else None
        )
        self.last_gyro_rotation_sample_count = gyro_sample_count
        self.last_gyro_rotation_angle_rad = gyro_angle

        result = self._estimate_best_step(
            self.previous,
            current,
            gyro_current_from_previous=gyro_current_from_previous,
        )
        if result is None:
            self.previous = current
            if gyro_previous_from_current is not None and gyro_sample_count >= 2:
                self.world_from_camera[:3, :3] = (
                    self.world_from_camera[:3, :3] @ gyro_previous_from_current
                )
                self.last_rotation_source = "GYRO_PROPAGATION"
                self.last_rotation_disagreement_rad = None
                self._update_gravity_reference(correction_policy="DEGRADED_RECOVERY")
                return self._status_result(
                    timestamp_us,
                    "DEGRADED",
                    rotation_step_rad=float(gyro_angle or 0.0),
                    gyro_delta_rad=gyro_angle,
                    message="visual correspondences unavailable; rotation propagated from gyroscope while translation is held",
                )
            self.last_rotation_source = "HOLD_LAST_POSE"
            self.last_rotation_disagreement_rad = None
            self._update_gravity_reference(correction_policy="DEGRADED_RECOVERY")
            return self._status_result(
                timestamp_us,
                "DEGRADED",
                message="insufficient RGB-D feature correspondences",
            )

        (
            current_from_previous,
            inlier_count,
            match_count,
            feature_mode,
            solver_mode,
            reprojection_rmse_px,
        ) = result
        self.last_feature_preprocess_mode = feature_mode
        translation_step = float(np.linalg.norm(current_from_previous[:3, 3]))
        rotation_step = rotation_angle(current_from_previous[:3, :3])
        rotation_disagreement = None
        if gyro_current_from_previous is not None:
            rotation_disagreement = rotation_angle(
                current_from_previous[:3, :3] @ gyro_current_from_previous.T
            )
        self.last_rotation_disagreement_rad = rotation_disagreement

        disagreement_limit = self._gyro_visual_disagreement_limit(gyro_angle)
        visual_is_implausible = (
            translation_step > self.max_translation_step_m
            or rotation_step > self.max_rotation_step_rad
        )
        visual_disagrees_with_gyro = (
            rotation_disagreement is not None
            and rotation_disagreement > disagreement_limit
        )
        if visual_is_implausible or visual_disagrees_with_gyro:
            self.previous = current
            if gyro_previous_from_current is not None and gyro_sample_count >= 2:
                self.world_from_camera[:3, :3] = (
                    self.world_from_camera[:3, :3] @ gyro_previous_from_current
                )
                self.last_rotation_source = "GYRO_PROPAGATION"
                self._update_gravity_reference(correction_policy="DEGRADED_RECOVERY")
                reason = (
                    "visual rotation disagrees with full gyroscope rotation"
                    if visual_disagrees_with_gyro
                    else "visual step rejected as implausibly large"
                )
                return self._status_result(
                    timestamp_us,
                    "DEGRADED",
                    inlier_count=inlier_count,
                    match_count=match_count,
                    translation_step_m=0.0,
                    rotation_step_rad=float(gyro_angle or 0.0),
                    gyro_delta_rad=gyro_angle,
                    message=(
                        f"{reason}; using gyro rotation hold, visual RMSE {reprojection_rmse_px:.2f}px"
                    ),
                )
            self.last_rotation_source = "REJECTED_VISUAL_HOLD"
            self._update_gravity_reference(correction_policy="DEGRADED_RECOVERY")
            return self._status_result(
                timestamp_us,
                "DEGRADED",
                inlier_count=inlier_count,
                match_count=match_count,
                translation_step_m=translation_step,
                rotation_step_rad=rotation_step,
                gyro_delta_rad=gyro_angle,
                message="visual pose step rejected and no complete gyro rotation was available",
            )

        previous_from_current = invert_transform(current_from_previous)
        old_translation = self.world_from_camera[:3, 3].copy()
        self.world_from_camera = self.world_from_camera @ previous_from_current
        delta_seconds = max(1e-6, (timestamp_us - self.previous.timestamp_us) / 1_000_000.0)
        new_translation = self.world_from_camera[:3, 3].copy()
        self.last_velocity = (new_translation - old_translation) / delta_seconds
        self.last_rotation_source = solver_mode

        # Visual odometry owns translation. Gravity adds only quiet roll/pitch leveling.
        self._update_gravity_reference(correction_policy="TRACKING_STABILIZE")
        self.previous = current
        return self._status_result(
            timestamp_us,
            "TRACKING",
            inlier_count=inlier_count,
            match_count=match_count,
            translation_step_m=translation_step,
            rotation_step_rad=rotation_step,
            gyro_delta_rad=gyro_angle,
            message=(
                None
                if rotation_disagreement is None
                else f"rotation verified against gyro; reprojection RMSE {reprojection_rmse_px:.2f}px"
            ),
        )

    def _try_initialize_gravity(self) -> bool:
        if len(self.acceleration_samples) < self.gravity_samples_required:
            return False
        samples = np.stack(tuple(self.acceleration_samples), axis=0)
        magnitudes = np.linalg.norm(samples, axis=1)
        self.gravity_std_mps2 = float(np.std(magnitudes))
        if self.gravity_std_mps2 > self.gravity_std_limit_mps2:
            return False
        mean_imu = np.mean(samples, axis=0)
        mean_color = self.color_from_imu[:3, :3] @ mean_imu
        rotation_world_from_camera = gravity_aligned_world_from_camera(mean_color)
        self.world_from_camera = make_transform(rotation_world_from_camera, [0.0, 0.0, 0.0])
        self._estimate_stationary_gyro_baseline()
        self.initialized = True
        return True

    def _update_gravity_reference(self, *, correction_policy: str) -> None:
        """Use gravity as a quiet-motion complementary roll/pitch reference.

        Visual odometry always owns translation. Gravity preserves horizontal yaw
        and modifies rotation only. During valid visual tracking the correction is
        deliberately tiny; degraded tracking permits a stronger recovery step.
        """
        self.last_gravity_correction_applied = False
        self.last_gravity_adjustment_state = "OFF"
        observation = self._stationary_gravity_observation()
        if observation is None:
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

        mean_color = self.color_from_imu[:3, :3] @ mean_imu
        norm = float(np.linalg.norm(mean_color))
        if norm <= 1e-9:
            self.last_gravity_correction_mode = "INVALID_GRAVITY"
            return
        up_camera = mean_color / norm
        current_rotation = self.world_from_camera[:3, :3]
        observed_up_world = current_rotation @ up_camera
        observed_up_world /= max(1e-12, float(np.linalg.norm(observed_up_world)))
        cosine = float(np.clip(np.dot(observed_up_world, WORLD_UP), -1.0, 1.0))
        tilt_error = math.acos(cosine)

        self.last_gravity_tracking_sample_count = sample_count
        self.last_gravity_tilt_error_rad = tilt_error
        self.last_gravity_direction_std_rad = direction_std
        self.last_gravity_stationary_duration_s = stationary_duration_s

        if correction_policy == "TRACKING_STABILIZE":
            delay_s = self.gravity_tracking_delay_s
            gain = self.gravity_tracking_gain
            max_step_rad = self.gravity_tracking_max_step_rad
            active_mode = "TRACKING_LEVELING_ACTIVE"
            ready_mode = "TRACKING_LEVELING_READY"
        elif correction_policy == "DEGRADED_RECOVERY":
            delay_s = self.gravity_recovery_delay_s
            gain = self.gravity_recovery_gain
            max_step_rad = self.gravity_recovery_max_step_rad
            active_mode = "DEGRADED_LEVELING_ACTIVE"
            ready_mode = "DEGRADED_LEVELING_READY"
        else:
            raise ValueError(f"unknown gravity correction policy: {correction_policy}")

        self.last_gravity_adjustment_state = "READY"
        if stationary_duration_s < delay_s:
            self.last_gravity_correction_mode = "WAITING_FOR_STABLE_GYRO"
            return
        if tilt_error <= math.radians(0.08):
            self.last_gravity_correction_mode = "GRAVITY_ALIGNED"
            return

        target_rotation = _gravity_target_rotation(current_rotation, up_camera)
        target_from_current = target_rotation @ current_rotation.T
        axis, total_angle = _rotation_axis_angle(target_from_current)
        if axis is None or total_angle <= 1e-8:
            self.last_gravity_correction_mode = "GRAVITY_ALIGNED"
            return

        correction_angle = min(max_step_rad, total_angle * gain)
        if correction_angle <= 1e-8:
            self.last_gravity_correction_mode = ready_mode
            return

        correction = _axis_angle_rotation(axis, correction_angle)
        self.world_from_camera[:3, :3] = correction @ current_rotation
        # Translation is intentionally untouched. The correction levels the head
        # attitude around its current position instead of rotating the world origin.
        self.last_gravity_correction_applied = True
        self.last_gravity_adjustment_state = "ACTIVE"
        self.last_gravity_correction_mode = active_mode

    def _estimate_stationary_gyro_baseline(self) -> None:
        """Estimate zero-rate bias and a robust residual noise ceiling at startup."""
        if len(self.gyro_samples) < 10:
            return
        latest_us = int(self.gyro_samples[-1][0])
        start_us = latest_us - max(self.gravity_tracking_window_us, 1_000_000)
        values = np.stack(
            [value for sample_time, value in self.gyro_samples if sample_time >= start_us],
            axis=0,
        )
        if len(values) < 10:
            return
        bias = np.median(values, axis=0)
        residual_norms = np.linalg.norm(values - bias, axis=1)
        median_residual = float(np.median(residual_norms))
        mad = float(np.median(np.abs(residual_norms - median_residual)))
        robust_sigma = 1.4826 * mad
        noise_ceiling = median_residual + 4.0 * robust_sigma
        self.gyro_bias_imu_radps = bias
        self.gravity_gyro_noise_floor_radps = noise_ceiling
        self.gravity_gyro_effective_limit_radps = float(
            np.clip(
                max(self.gravity_gyro_limit_radps, 1.5 * noise_ceiling),
                0.008,
                0.03,
            )
        )

    def _stationary_gravity_observation(
        self,
    ) -> tuple[np.ndarray, int, float, int, int] | None:
        if not self.acceleration_history or not self.gyro_samples:
            return None

        # Accelerometer and gyro samples come from the same physical IMU. Use
        # their latest overlapping time instead of assuming the RGB timestamp is
        # in exactly the same domain.
        window_end_us = min(
            int(self.acceleration_history[-1][0]),
            int(self.gyro_samples[-1][0]),
        )
        window_start_us = window_end_us - self.gravity_tracking_window_us
        acceleration = [
            value
            for sample_time, value in self.acceleration_history
            if window_start_us <= sample_time <= window_end_us
        ]
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

        gyroscope = [
            value
            for sample_time, value in self.gyro_samples
            if window_start_us <= sample_time <= window_end_us
        ]
        if len(gyroscope) < 2:
            return None
        gyro_array = np.stack(gyroscope, axis=0)
        residual = gyro_array - self.gyro_bias_imu_radps
        residual_norms = np.linalg.norm(residual, axis=1)
        rms_gyro_norm = float(np.sqrt(np.mean(np.square(residual_norms))))
        p95_gyro_norm = float(np.percentile(residual_norms, 95.0))
        self.last_gravity_gyro_rms_radps = rms_gyro_norm
        self.last_gravity_gyro_p95_radps = p95_gyro_norm
        if p95_gyro_norm > self.gravity_gyro_effective_limit_radps:
            return None
        return (
            np.mean(samples, axis=0),
            len(acceleration),
            direction_std,
            window_start_us,
            window_end_us,
        )

    def _extract_feature_sets(self, gray: np.ndarray) -> dict[str, _FeatureSet]:
        raw_keypoints, raw_descriptors = self.orb.detectAndCompute(gray, None)
        features = {
            "RAW_BASELINE": _FeatureSet(
                name="RAW_BASELINE",
                keypoints=raw_keypoints,
                descriptors=raw_descriptors,
            )
        }
        if (
            self.feature_preprocess_mode == "adaptive_circular_lcn"
            and len(raw_keypoints) < self.lcn_raw_keypoint_trigger
            and self._should_use_lcn(gray)
        ):
            normalized = _circular_local_contrast_normalize(gray, self._lcn_kernel)
            normalized_keypoints, normalized_descriptors = self.orb.detectAndCompute(
                normalized,
                None,
            )
            features["CIRCULAR_LCN"] = _FeatureSet(
                name="CIRCULAR_LCN",
                keypoints=normalized_keypoints,
                descriptors=normalized_descriptors,
            )
        return features

    def _should_use_lcn(self, gray: np.ndarray) -> bool:
        median = float(np.median(gray))
        low, high = np.percentile(gray, [10.0, 90.0])
        contrast_span = float(high - low)
        return median <= self.lcn_low_light_median or contrast_span <= self.lcn_low_contrast_span

    def _estimate_best_step(
        self,
        previous: _FrameState,
        current: _FrameState,
        *,
        gyro_current_from_previous: np.ndarray | None = None,
    ) -> tuple[np.ndarray, int, int, str, str, float] | None:
        raw_previous = previous.features.get("RAW_BASELINE")
        raw_current = current.features.get("RAW_BASELINE")
        raw = None
        if raw_previous is not None and raw_current is not None:
            raw = self._estimate_step_for_features(
                previous,
                raw_previous,
                raw_current,
                gyro_current_from_previous=gyro_current_from_previous,
            )

        normalized_previous = previous.features.get("CIRCULAR_LCN")
        normalized_current = current.features.get("CIRCULAR_LCN")
        normalized_available = normalized_previous is not None and normalized_current is not None

        if raw is not None and (not normalized_available or raw[1] >= self.lcn_raw_inlier_accept):
            transform, inliers, matches, solver_mode, rmse = raw
            return transform, inliers, matches, "RAW_BASELINE", solver_mode, rmse

        normalized = None
        if normalized_available:
            normalized = self._estimate_step_for_features(
                previous,
                normalized_previous,
                normalized_current,
                gyro_current_from_previous=gyro_current_from_previous,
            )

        if raw is None and normalized is None:
            return None
        if raw is None:
            transform, inliers, matches, solver_mode, rmse = normalized
            return transform, inliers, matches, "CIRCULAR_LCN_FALLBACK", solver_mode, rmse
        if normalized is None:
            transform, inliers, matches, solver_mode, rmse = raw
            return transform, inliers, matches, "RAW_BASELINE", solver_mode, rmse

        raw_transform, raw_inliers, raw_matches, raw_solver, raw_rmse = raw
        norm_transform, norm_inliers, norm_matches, norm_solver, norm_rmse = normalized
        required_inliers = max(
            raw_inliers + 6,
            int(math.ceil(raw_inliers * (1.0 + self.lcn_selection_margin))),
        )
        if norm_inliers >= required_inliers and norm_rmse <= raw_rmse * 1.15:
            return (
                norm_transform,
                norm_inliers,
                norm_matches,
                "CIRCULAR_LCN_SELECTED",
                norm_solver,
                norm_rmse,
            )
        return (
            raw_transform,
            raw_inliers,
            raw_matches,
            "RAW_BASELINE",
            raw_solver,
            raw_rmse,
        )

    def _estimate_step_for_features(
        self,
        previous: _FrameState,
        previous_features: _FeatureSet,
        current_features: _FeatureSet,
        *,
        gyro_current_from_previous: np.ndarray | None = None,
    ) -> tuple[np.ndarray, int, int, str, float] | None:
        if previous_features.descriptors is None or current_features.descriptors is None:
            return None
        if (
            len(previous_features.keypoints) < self.min_inliers
            or len(current_features.keypoints) < self.min_inliers
        ):
            return None
        pairs = self.matcher.knnMatch(
            previous_features.descriptors,
            current_features.descriptors,
            k=2,
        )
        matches = [
            first
            for pair in pairs
            if len(pair) >= 2
            for first, second in [pair[:2]]
            if first.distance < 0.72 * second.distance
        ]
        if len(matches) < self.min_inliers:
            return None

        object_points: list[list[float]] = []
        image_points: list[list[float]] = []
        fx = float(self.camera_matrix[0, 0])
        fy = float(self.camera_matrix[1, 1])
        cx = float(self.camera_matrix[0, 2])
        cy = float(self.camera_matrix[1, 2])
        height, width = previous.depth_m.shape
        for match in matches:
            u, v = previous_features.keypoints[match.queryIdx].pt
            x_pixel = int(round(u))
            y_pixel = int(round(v))
            if x_pixel < 0 or y_pixel < 0 or x_pixel >= width or y_pixel >= height:
                continue
            depth = float(previous.depth_m[y_pixel, x_pixel])
            if not (0.15 <= depth <= 8.0):
                continue
            x = (u - cx) * depth / fx
            y = (v - cy) * depth / fy
            object_points.append([x, y, depth])
            current_u, current_v = current_features.keypoints[match.trainIdx].pt
            image_points.append([current_u, current_v])

        if len(object_points) < self.min_inliers:
            return None
        object_array = np.asarray(object_points, dtype=np.float32)
        image_array = np.asarray(image_points, dtype=np.float32)

        candidates: list[tuple[np.ndarray, int, int, str, float]] = []
        baseline = self._solve_pnp_candidate(
            object_array,
            image_array,
            solver_mode="VISUAL_EPNP",
            initial_rotation=None,
            initial_translation=None,
        )
        if baseline is not None:
            candidates.append(baseline)

        gyro_angle = (
            rotation_angle(gyro_current_from_previous)
            if gyro_current_from_previous is not None
            else 0.0
        )
        should_try_seeded = (
            gyro_current_from_previous is not None
            and (
                baseline is None
                or gyro_angle >= self.gyro_seed_min_angle_rad
                or baseline[1] < self.lcn_raw_inlier_accept
            )
        )
        if should_try_seeded:
            initial_translation = (
                baseline[0][:3, 3] if baseline is not None else np.zeros(3, dtype=np.float64)
            )
            seeded = self._solve_pnp_candidate(
                object_array,
                image_array,
                solver_mode="VISUAL_GYRO_SEEDED_PNP",
                initial_rotation=gyro_current_from_previous,
                initial_translation=initial_translation,
            )
            if seeded is not None:
                candidates.append(seeded)

        if not candidates:
            return None
        best = candidates[0]
        for candidate in candidates[1:]:
            _, inliers, _, _, rmse = candidate
            _, best_inliers, _, _, best_rmse = best
            if inliers >= best_inliers + 4:
                best = candidate
            elif inliers >= best_inliers - 2 and rmse < best_rmse * 0.80:
                best = candidate
        return best

    def _solve_pnp_candidate(
        self,
        object_array: np.ndarray,
        image_array: np.ndarray,
        *,
        solver_mode: str,
        initial_rotation: np.ndarray | None,
        initial_translation: np.ndarray | None,
    ) -> tuple[np.ndarray, int, int, str, float] | None:
        if initial_rotation is None:
            success, rotation_vector, translation, inliers = cv2.solvePnPRansac(
                object_array,
                image_array,
                self.camera_matrix,
                None,
                iterationsCount=120,
                reprojectionError=2.5,
                confidence=0.995,
                flags=cv2.SOLVEPNP_EPNP,
            )
        else:
            rotation_vector, _ = cv2.Rodrigues(
                np.asarray(initial_rotation, dtype=np.float64)
            )
            translation = np.asarray(
                initial_translation if initial_translation is not None else np.zeros(3),
                dtype=np.float64,
            ).reshape(3, 1)
            success, rotation_vector, translation, inliers = cv2.solvePnPRansac(
                object_array,
                image_array,
                self.camera_matrix,
                None,
                rotation_vector,
                translation,
                True,
                160,
                3.0,
                0.995,
                flags=cv2.SOLVEPNP_ITERATIVE,
            )
        if not success or inliers is None or len(inliers) < self.min_inliers:
            return None
        rotation, _ = cv2.Rodrigues(rotation_vector)
        transform = make_transform(rotation, translation.reshape(3))
        indices = inliers.reshape(-1)
        projected, _ = cv2.projectPoints(
            object_array[indices],
            rotation_vector,
            translation,
            self.camera_matrix,
            None,
        )
        residual = projected.reshape(-1, 2) - image_array[indices]
        rmse = float(np.sqrt(np.mean(np.sum(residual * residual, axis=1))))
        return transform, int(len(inliers)), int(len(object_array)), solver_mode, rmse

    def _integrated_gyro_rotation(
        self,
        start_us: int,
        end_us: int,
    ) -> tuple[np.ndarray | None, int]:
        if end_us <= start_us or len(self.gyro_samples) < 2:
            return None, 0
        samples = list(self.gyro_samples)
        selected: list[tuple[int, np.ndarray]] = []
        previous_sample: tuple[int, np.ndarray] | None = None
        for sample in samples:
            if sample[0] < start_us:
                previous_sample = sample
                continue
            if previous_sample is not None and not selected:
                selected.append(previous_sample)
            selected.append(sample)
            if sample[0] >= end_us:
                break
        if len(selected) < 2:
            return None, len(selected)

        rotation_previous_from_current = np.eye(3, dtype=np.float64)
        integrated_segments = 0
        color_from_imu_rotation = self.color_from_imu[:3, :3]
        for (left_time, left), (right_time, right) in zip(selected, selected[1:]):
            segment_start = max(start_us, left_time)
            segment_end = min(end_us, right_time)
            if segment_end <= segment_start or right_time <= left_time:
                continue
            alpha_start = (segment_start - left_time) / (right_time - left_time)
            alpha_end = (segment_end - left_time) / (right_time - left_time)
            left_interp = left + (right - left) * alpha_start
            right_interp = left + (right - left) * alpha_end
            mean_imu = 0.5 * (left_interp + right_interp) - self.gyro_bias_imu_radps
            mean_camera = color_from_imu_rotation @ mean_imu
            delta_seconds = (segment_end - segment_start) / 1_000_000.0
            delta_vector = mean_camera * delta_seconds
            angle = float(np.linalg.norm(delta_vector))
            if angle > 1e-12:
                rotation_previous_from_current = (
                    rotation_previous_from_current
                    @ _axis_angle_rotation(delta_vector / angle, angle)
                )
            integrated_segments += 1
        if integrated_segments == 0:
            return None, len(selected)
        return rotation_previous_from_current, len(selected)

    def _gyro_visual_disagreement_limit(self, gyro_angle: float | None) -> float:
        angle = float(gyro_angle or 0.0)
        return self.gyro_visual_disagreement_base_rad + self.gyro_visual_disagreement_scale * angle

    def _status_result(
        self,
        timestamp_us: int,
        tracking_state: str,
        *,
        inlier_count: int = 0,
        match_count: int = 0,
        translation_step_m: float = 0.0,
        rotation_step_rad: float = 0.0,
        gyro_delta_rad: float | None = None,
        message: str | None,
    ) -> PoseResult:
        return PoseResult(
            timestamp_us=timestamp_us,
            world_from_camera=self.world_from_camera.copy(),
            velocity_world_mps=(
                self.last_velocity.copy()
                if tracking_state == "TRACKING"
                else np.zeros(3, dtype=np.float64)
            ),
            tracking_state=tracking_state,
            inlier_count=inlier_count,
            match_count=match_count,
            translation_step_m=translation_step_m,
            rotation_step_rad=rotation_step_rad,
            gravity_sample_count=len(self.acceleration_samples),
            gravity_std_mps2=self.gravity_std_mps2,
            gyro_delta_rad=gyro_delta_rad,
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
        )


def _circular_kernel(radius_px: int) -> np.ndarray:
    radius = max(1, int(radius_px))
    coordinates = np.arange(-radius, radius + 1, dtype=np.float32)
    yy, xx = np.meshgrid(coordinates, coordinates, indexing="ij")
    kernel = ((xx * xx + yy * yy) <= float(radius * radius)).astype(np.float32)
    kernel_sum = float(np.sum(kernel))
    if kernel_sum <= 0.0:
        raise ValueError("circular normalization kernel is empty")
    return kernel / kernel_sum


def _circular_local_contrast_normalize(gray: np.ndarray, kernel: np.ndarray) -> np.ndarray:
    """Normalize local luminance using circular convolution neighborhoods.

    Gain is bounded so dark sensor noise is not amplified without limit. The
    original raw frame remains available as the baseline feature path.
    """
    image = np.asarray(gray, dtype=np.float32)
    local_mean = cv2.filter2D(
        image,
        cv2.CV_32F,
        kernel,
        borderType=cv2.BORDER_REFLECT101,
    )
    local_second_moment = cv2.filter2D(
        image * image,
        cv2.CV_32F,
        kernel,
        borderType=cv2.BORDER_REFLECT101,
    )
    variance = np.maximum(local_second_moment - local_mean * local_mean, 0.0)
    local_std = np.sqrt(variance)
    gain = np.minimum(3.0, 42.0 / np.maximum(local_std, 8.0))
    normalized = 128.0 + (image - local_mean) * gain
    return np.clip(normalized, 0.0, 255.0).astype(np.uint8)


def _axis_angle_rotation(axis: np.ndarray, angle: float) -> np.ndarray:
    x, y, z = np.asarray(axis, dtype=np.float64)
    cosine = math.cos(angle)
    sine = math.sin(angle)
    one_minus_cosine = 1.0 - cosine
    return np.array(
        [
            [
                cosine + x * x * one_minus_cosine,
                x * y * one_minus_cosine - z * sine,
                x * z * one_minus_cosine + y * sine,
            ],
            [
                y * x * one_minus_cosine + z * sine,
                cosine + y * y * one_minus_cosine,
                y * z * one_minus_cosine - x * sine,
            ],
            [
                z * x * one_minus_cosine - y * sine,
                z * y * one_minus_cosine + x * sine,
                cosine + z * z * one_minus_cosine,
            ],
        ],
        dtype=np.float64,
    )


def _horizontal_heading(vector: np.ndarray) -> np.ndarray | None:
    heading = np.asarray(vector, dtype=np.float64) - WORLD_UP * float(np.dot(vector, WORLD_UP))
    norm = float(np.linalg.norm(heading))
    if norm <= 1e-8:
        return None
    return heading / norm


def _gravity_target_rotation(current_rotation: np.ndarray, up_camera: np.ndarray) -> np.ndarray:
    """Return an absolute gravity-aligned camera rotation while preserving yaw."""
    current = np.asarray(current_rotation, dtype=np.float64)
    up_c = np.asarray(up_camera, dtype=np.float64)
    up_c /= max(1e-12, float(np.linalg.norm(up_c)))

    heading_world = _horizontal_heading(current[:, 2])
    if heading_world is None:
        left_world = _horizontal_heading(-current[:, 0])
        if left_world is not None:
            heading_world = np.cross(left_world, WORLD_UP)
            heading_world /= max(1e-12, float(np.linalg.norm(heading_world)))
    if heading_world is None:
        return gravity_aligned_world_from_camera(up_c)

    forward_hint_camera = np.array([0.0, 0.0, 1.0], dtype=np.float64)
    forward_camera = forward_hint_camera - up_c * float(np.dot(forward_hint_camera, up_c))
    if float(np.linalg.norm(forward_camera)) < 1e-6:
        return gravity_aligned_world_from_camera(up_c)
    forward_camera /= max(1e-12, float(np.linalg.norm(forward_camera)))
    left_camera = np.cross(up_c, forward_camera)
    left_camera /= max(1e-12, float(np.linalg.norm(left_camera)))
    forward_camera = np.cross(left_camera, up_c)
    forward_camera /= max(1e-12, float(np.linalg.norm(forward_camera)))

    left_world = np.cross(WORLD_UP, heading_world)
    left_world /= max(1e-12, float(np.linalg.norm(left_world)))
    heading_world = np.cross(left_world, WORLD_UP)
    heading_world /= max(1e-12, float(np.linalg.norm(heading_world)))

    camera_basis = np.column_stack((forward_camera, left_camera, up_c))
    world_basis = np.column_stack((heading_world, left_world, WORLD_UP))
    return world_basis @ camera_basis.T


def _rotation_axis_angle(rotation: np.ndarray) -> tuple[np.ndarray | None, float]:
    matrix = np.asarray(rotation, dtype=np.float64)
    angle = rotation_angle(matrix)
    if angle <= 1e-10:
        return None, 0.0
    axis = np.array(
        [
            matrix[2, 1] - matrix[1, 2],
            matrix[0, 2] - matrix[2, 0],
            matrix[1, 0] - matrix[0, 1],
        ],
        dtype=np.float64,
    )
    axis_norm = float(np.linalg.norm(axis))
    if axis_norm <= 1e-8:
        # The near-pi case is uncommon for gravity recovery, but an eigenvector
        # of R with eigenvalue one remains a valid rotation axis.
        values, vectors = np.linalg.eig(matrix)
        index = int(np.argmin(np.abs(values - 1.0)))
        axis = np.real(vectors[:, index])
        axis_norm = float(np.linalg.norm(axis))
        if axis_norm <= 1e-8:
            return None, angle
    return axis / axis_norm, angle
