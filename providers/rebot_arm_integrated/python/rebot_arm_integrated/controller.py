from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import copy
import math
import threading
import time

import numpy as np

from .basic_client import BasicControllerClient, LeaseLostError
from .command_semantics import LatchedEndpointCommand, synchronized_velocity_limits
from .contact import (
    TorqueBaseline,
    cartesian_wrench_to_joint_budget,
    force_position_ratios,
    isotropic_wrench_to_joint_budget,
    torque_limit_violations,
)
from .hybrid import MIT_SETTLE, POS_VEL_APPROACH, HybridApproachPolicy
from .http_client import HttpStatusError
from .kinematics import ArmKinematics, matrix_rpy, rotation_vector, rpy_matrix, transform
from .modes import CONTACT_WORK, MODE_SPECS, PRESS_MIT, TRANSIT_SPEED, normalize_execution_mode
from .planning import PlanPreview, build_waypoint_preview, solve_cartesian_continuity
from .scene import SceneSnapshot


MODE_MIT = "IMPEDANCE"
CONTROL_MODE = "MIT_CARTESIAN_BRINGUP"
INTERACTION_ONE_SHOT = "ONE_SHOT"
INTERACTION_HOLD_LB = "HOLD_LB"
IK_POSITION_3DOF = "POSITION_3DOF"
IK_POSE_6DOF = "POSE_6DOF"
SUPPORTED_INTERACTIONS = {INTERACTION_ONE_SHOT, INTERACTION_HOLD_LB}
SUPPORTED_IK_MODES = {IK_POSITION_3DOF, IK_POSE_6DOF}
GRIPPER_MIT = "MIT"
GRIPPER_POS_TOR = "POS_TOR"
GRIPPER_OPEN = "OPEN"
GRIPPER_CLOSE = "CLOSE"
SUPPORTED_GRIPPER_MODES = {GRIPPER_MIT, GRIPPER_POS_TOR}
SUPPORTED_GRIPPER_ACTIONS = {GRIPPER_OPEN, GRIPPER_CLOSE}


class PlanningRejected(RuntimeError):
    """A candidate plan was rejected without implying a controller or transport fault."""


@dataclass
class InputState:
    received_monotonic: float = 0.0
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    roll: float = 0.0
    pitch: float = 0.0
    yaw: float = 0.0
    lb_pressed: bool = False
    rb_open_pressed: bool = False
    rt_close_pressed: bool = False


@dataclass
class TrajectoryPlan:
    execution_mode: str
    interaction_mode: str
    ik_mode: str
    q_start: np.ndarray
    qd_start: np.ndarray
    q_goal: np.ndarray
    segment_started_monotonic: float
    duration_s: float
    controlled_start: np.ndarray
    controlled_goal: np.ndarray
    position_residual_m: float
    orientation_residual_rad: float
    ik_iterations: int
    sigma_min: float
    target_revision: int
    replan_count: int = 0
    last_replan_monotonic: float = 0.0
    frames_attempted: int = 0
    frames_sent: int = 0
    frames_skipped: int = 0
    final_frame_sent: bool = False


class IntegratedController:
    """Cartesian planning controller with explicitly gated execution backends.

    Integrated owns the configured MIT waypoint stream. ONE_SHOT commits on the LB
    rising edge. PRESS_MIT and one-shot TRANSIT_SPEED return to gravity-float after
    arrival. HOLD_LB replans only when the staged target changes, preserves the last
    accepted endpoint, and returns to gravity-float when LB is released. CONTACT_WORK
    is one-shot only and uses an explicitly captured posture-local torque baseline.
    Joint-7 gripper testing supports operator-held MIT or POS_TOR commands.
    """

    def __init__(self, config: dict[str, Any], basic: BasicControllerClient):
        self.config = copy.deepcopy(config)
        self.basic = basic
        self.lock = threading.RLock()
        self.command_gate_lock = threading.RLock()
        self.stop_event = threading.Event()
        self.control_thread: threading.Thread | None = None
        self.lease_thread: threading.Thread | None = None
        self.trajectory_thread: threading.Thread | None = None

        self.residency = "WARM"
        self.health = "HEALTHY"
        self.ready = False
        self.control_state = "IDLE_FLOAT"
        self.engaged = False

        runtime = self.config["runtime"]
        self.execution_mode = normalize_execution_mode(runtime["execution_mode"])
        self.interaction_mode = str(runtime["interaction_mode"]).upper()
        if self.execution_mode == CONTACT_WORK:
            self.interaction_mode = INTERACTION_ONE_SHOT
        self.ik_mode = str(runtime["ik_mode"]).upper()
        if self.interaction_mode not in SUPPORTED_INTERACTIONS:
            raise ValueError(f"unsupported interaction mode {self.interaction_mode}")
        if self.ik_mode not in SUPPORTED_IK_MODES:
            raise ValueError(f"unsupported IK mode {self.ik_mode}")
        self.duration_s = float(runtime["duration_s"])
        self.replan_interval_s = float(runtime["replan_interval_s"])
        self.kp_multiplier = float(runtime["kp_multiplier"])
        self.tool_offset_xyz_m = np.asarray(runtime["controlled_frame_offset_xyz_m"], dtype=float)
        self.tool_offset_rpy_rad = np.asarray(runtime["controlled_frame_offset_rpy_rad"], dtype=float)
        self.payload_mass_kg = float(runtime["payload_mass_kg"])
        self.payload_com_tool_m = np.asarray(runtime["payload_com_tool_m"], dtype=float)
        gripper = self.config["gripper"]
        self.gripper_mode = str(gripper["mode"]).strip().upper()
        self.gripper_open_position_rad = float(gripper["open_position_rad"])
        self.gripper_closed_position_rad = float(gripper["closed_position_rad"])
        self.gripper_velocity_limit_rad_s = float(gripper["velocity_limit_rad_s"])
        self.gripper_torque_limit_ratio = float(gripper["torque_limit_ratio"])
        self.gripper_mit_kp = float(gripper["mit_kp"])
        self.gripper_mit_kd = float(gripper["mit_kd"])
        self.gripper_keepalive_hz = float(gripper["keepalive_hz"])
        self._validate_runtime_values()

        self.input = InputState()
        self._lb_previous = False
        self.commit_requested = False
        self.replan_requested = False
        self.gripper_gamepad_action: str | None = None
        self.gripper_ui_action: str | None = None
        self.gripper_active_action: str | None = None
        self.gripper_target_rad: float | None = None
        self.gripper_last_send_monotonic = 0.0
        self.gripper_command_count = 0
        self.gripper_stop_count = 0
        self.gripper_last_error: str | None = None
        self.gripper_fault_latched = False
        self.commit_count = 0
        self.fault_reason: str | None = None
        self.last_error: str | None = None

        self.basic_state: dict[str, Any] = {}
        self.basic_model: dict[str, Any] = {}
        self.kinematics: ArmKinematics | None = None
        self.last_state_poll = 0.0
        self.last_state_success = 0.0

        self.staged_target: np.ndarray | None = None
        self.last_target_update = 0.0
        self.last_commit_origin: np.ndarray | None = None
        self.last_committed_target: np.ndarray | None = None
        self.last_target_clamped = False
        self.last_position_residual_m: float | None = None
        self.last_orientation_residual_rad: float | None = None
        self.last_ik_iterations: int | None = None
        self.last_sigma_min: float | None = None
        self.goal_q: np.ndarray | None = None
        self.commanded_q: np.ndarray | None = None
        self.commanded_qd: np.ndarray | None = None
        self.trajectory: TrajectoryPlan | None = None
        self.last_completed_trajectory: dict[str, Any] | None = None
        self.scene: SceneSnapshot | None = None
        self.scene_source: str | None = None
        self.scene_received_monotonic = 0.0
        self.target_revision = 0
        self.last_preview: PlanPreview | None = None
        self.last_preview_context: dict[str, Any] | None = None
        self.preview_count = 0
        self.preview_rejected_count = 0
        self.latched_endpoint: LatchedEndpointCommand | None = None
        self.hybrid_policy: HybridApproachPolicy | None = None
        self.hybrid_started_monotonic = 0.0
        self.torque_baseline: TorqueBaseline | None = None
        self.baseline_capture_state = "NOT_CAPTURED"
        self.baseline_capture_error: str | None = None
        self.baseline_captured_monotonic = 0.0
        self.contact_torque_limit_ratios: np.ndarray | None = None
        self.contact_effective_joint_budget_nm: np.ndarray | None = None
        self.contact_residual_nm: np.ndarray | None = None
        self.contact_limit_violations: list[int] = []
        self.contact_saturated_joint_indices: list[int] = []
        self.contact_saturation_count = 0
        self.contact_saturation_reason: str | None = None

        self.command_count = 0
        self.rejected_count = 0
        self.last_command_latency_ms: float | None = None
        self.max_command_latency_ms = 0.0
        self.last_command_sent_monotonic = 0.0
        self.max_send_lateness_ms = 0.0
        self.max_tracking_error_rad = 0.0
        self.last_tracking_error_rad = 0.0
        self.live_replan_count = 0
        self.last_replan_duration_ms: float | None = None
        self.max_replan_duration_ms = 0.0

        self.lease_state = "NONE"
        self.last_lease_attempt = 0.0
        self.last_lease_renew = 0.0
        self.lease_renew_success_count = 0
        self.lease_renew_failure_count = 0
        self.lease_acquire_failure_count = 0
        self.lease_renew_latency_ms: float | None = None
        self.max_lease_renew_latency_ms = 0.0

        self.float_confirmed = False
        self.float_request_count = 0
        self.float_failure_count = 0
        self.last_float_confirmed = 0.0
        self.last_float_reason: str | None = None

        self.manager_registered = False
        self.fabric_ready = False
        self.motion_inhibited = False
        self.motion_inhibit_owners: list[dict[str, Any]] = []
        self.platform_errors: dict[str, str | None] = {
            "manager": None,
            "fabric_publish": None,
            "fabric_consume": None,
        }
        self.external_target_update_count = 0
        self.last_external_target_source: str | None = None
        self.last_external_target_monotonic = 0.0
        self.last_external_target_metadata: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Runtime configuration
    # ------------------------------------------------------------------
    def _validate_runtime_values(self) -> None:
        limits = self.config["runtime_limits"]
        if not float(limits["duration_min_s"]) <= self.duration_s <= float(limits["duration_max_s"]):
            raise ValueError("duration is outside the configured runtime range")
        if not float(limits["replan_interval_min_s"]) <= self.replan_interval_s <= float(limits["replan_interval_max_s"]):
            raise ValueError("replan interval is outside the configured runtime range")
        if not float(limits["kp_multiplier_min"]) <= self.kp_multiplier <= float(limits["kp_multiplier_max"]):
            raise ValueError("Kp multiplier is outside the configured runtime range")
        if self.tool_offset_xyz_m.shape != (3,) or not np.all(np.isfinite(self.tool_offset_xyz_m)):
            raise ValueError("controlled-frame XYZ offset must contain three finite values")
        if self.tool_offset_rpy_rad.shape != (3,) or not np.all(np.isfinite(self.tool_offset_rpy_rad)):
            raise ValueError("controlled-frame RPY offset must contain three finite values")
        if self.payload_com_tool_m.shape != (3,) or not np.all(np.isfinite(self.payload_com_tool_m)):
            raise ValueError("payload COM must contain three finite values")
        if not np.isfinite(self.payload_mass_kg) or self.payload_mass_kg < 0.0:
            raise ValueError("payload mass must be finite and non-negative")
        if self.gripper_mode not in SUPPORTED_GRIPPER_MODES:
            raise ValueError(f"unsupported gripper mode {self.gripper_mode}")
        if abs(self.gripper_open_position_rad - self.gripper_closed_position_rad) < 1e-6:
            raise ValueError("gripper open and closed positions must be distinct")
        if self.gripper_velocity_limit_rad_s <= 0.0:
            raise ValueError("gripper velocity limit must be positive")
        if not 0.0 < self.gripper_torque_limit_ratio <= 1.0:
            raise ValueError("gripper torque limit ratio must be in (0, 1]")
        if self.gripper_mit_kp <= 0.0 or self.gripper_mit_kd < 0.0:
            raise ValueError("gripper MIT gains are invalid")
        if not 2.0 <= self.gripper_keepalive_hz <= 20.0:
            raise ValueError("gripper keepalive rate must be in [2, 20] Hz")

    def _tool_to_control_locked(self) -> np.ndarray:
        return transform(self.tool_offset_xyz_m, rpy_matrix(self.tool_offset_rpy_rad))

    def set_runtime_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        payload_update = False
        with self.lock:
            if self.trajectory is not None:
                raise RuntimeError("runtime settings cannot change during active motion")
            if "interaction_mode" in payload:
                value = str(payload["interaction_mode"]).strip().upper()
                if value not in SUPPORTED_INTERACTIONS:
                    raise ValueError(f"unsupported interaction mode {value}")
                self.interaction_mode = value
            if "execution_mode" in payload:
                self.execution_mode = normalize_execution_mode(payload["execution_mode"])
            if self.execution_mode == CONTACT_WORK:
                self.interaction_mode = INTERACTION_ONE_SHOT
            if "ik_mode" in payload:
                value = str(payload["ik_mode"]).strip().upper()
                if value not in SUPPORTED_IK_MODES:
                    raise ValueError(f"unsupported IK mode {value}")
                self.ik_mode = value
            if "duration_s" in payload:
                self.duration_s = float(payload["duration_s"])
            if "replan_interval_s" in payload:
                self.replan_interval_s = float(payload["replan_interval_s"])
            if "kp_multiplier" in payload:
                self.kp_multiplier = float(payload["kp_multiplier"])
            if "controlled_frame_offset_xyz_m" in payload:
                self.tool_offset_xyz_m = np.asarray(payload["controlled_frame_offset_xyz_m"], dtype=float)
            if "controlled_frame_offset_rpy_rad" in payload:
                self.tool_offset_rpy_rad = np.asarray(payload["controlled_frame_offset_rpy_rad"], dtype=float)
            if "payload_mass_kg" in payload:
                self.payload_mass_kg = float(payload["payload_mass_kg"])
                payload_update = True
            if "payload_com_tool_m" in payload:
                self.payload_com_tool_m = np.asarray(payload["payload_com_tool_m"], dtype=float)
                payload_update = True
            contact_budget_key = (
                "contact_torque_budget_nm"
                if "contact_torque_budget_nm" in payload
                else "task_torque_budget_nm"
                if "task_torque_budget_nm" in payload
                else None
            )
            if contact_budget_key is not None:
                raw_budget = payload[contact_budget_key]
                if raw_budget is None:
                    self.config["contact"]["task_torque_budget_nm"] = None
                else:
                    budget = np.asarray(raw_budget, dtype=float)
                    if budget.shape != (6,) or not np.all(np.isfinite(budget)) or np.any(budget <= 0.0):
                        raise ValueError("contact torque budget must contain six positive finite values")
                    self.config["contact"]["task_torque_budget_nm"] = budget.tolist()
                self.config["contact"]["budget_mode"] = "JOINT_6"
                self.contact_torque_limit_ratios = None
                self.contact_effective_joint_budget_nm = None
                self.contact_residual_nm = None
                self.contact_limit_violations = []
                self.contact_saturated_joint_indices = []
                self.contact_saturation_reason = None
            if "contact_budget_mode" in payload:
                budget_mode = str(payload["contact_budget_mode"]).strip().upper()
                if budget_mode not in {"JOINT_6", "WRENCH_6", "ISOTROPIC_2"}:
                    raise ValueError("contact budget mode must be JOINT_6, WRENCH_6, or ISOTROPIC_2")
                self.config["contact"]["budget_mode"] = budget_mode
                self.contact_torque_limit_ratios = None
                self.contact_effective_joint_budget_nm = None
                self.contact_residual_nm = None
                self.contact_limit_violations = []
                self.contact_saturated_joint_indices = []
                self.contact_saturation_reason = None
            for payload_key, config_key in (
                ("contact_wrench_force_budget_n", "wrench_force_budget_n"),
                ("contact_wrench_torque_budget_nm", "wrench_torque_budget_nm"),
            ):
                if payload_key in payload:
                    raw = payload[payload_key]
                    if raw is None:
                        self.config["contact"][config_key] = None
                    else:
                        values = np.asarray(raw, dtype=float)
                        if (
                            values.shape != (3,)
                            or not np.all(np.isfinite(values))
                            or np.any(values < 0.0)
                        ):
                            raise ValueError(f"{payload_key} must contain three non-negative finite values")
                        self.config["contact"][config_key] = values.tolist()
                    self.contact_torque_limit_ratios = None
                    self.contact_effective_joint_budget_nm = None
                    self.contact_residual_nm = None
                    self.contact_limit_violations = []
                    self.contact_saturated_joint_indices = []
                    self.contact_saturation_reason = None
            for payload_key, config_key in (
                ("contact_isotropic_force_budget_n", "isotropic_force_budget_n"),
                ("contact_isotropic_torque_budget_nm", "isotropic_torque_budget_nm"),
            ):
                if payload_key in payload:
                    value = payload[payload_key]
                    if value is None:
                        self.config["contact"][config_key] = None
                    else:
                        number = float(value)
                        if not np.isfinite(number) or number < 0.0:
                            raise ValueError(f"{payload_key} must be non-negative and finite")
                        self.config["contact"][config_key] = number
                    self.contact_torque_limit_ratios = None
                    self.contact_effective_joint_budget_nm = None
                    self.contact_residual_nm = None
                    self.contact_limit_violations = []
                    self.contact_saturated_joint_indices = []
                    self.contact_saturation_reason = None
            self._validate_runtime_values()
            if payload:
                self._invalidate_preview_locked()
            if payload_update:
                self.torque_baseline = None
                self.baseline_capture_state = "INVALIDATED_BY_PAYLOAD_CHANGE"
                self.contact_torque_limit_ratios = None
                self.contact_effective_joint_budget_nm = None
                self.contact_residual_nm = None
                self.contact_limit_violations = []
                self.contact_saturated_joint_indices = []
                self.contact_saturation_reason = None
            if self.kinematics is not None and self.basic_state:
                measured = self._measured_positions_locked()[:6]
                if any(key in payload for key in {"controlled_frame_offset_xyz_m", "controlled_frame_offset_rpy_rad"}):
                    self.staged_target = self.kinematics.controlled_frame(measured, self._tool_to_control_locked())
                    self.last_target_update = time.monotonic()
                    self._invalidate_preview_locked()
        if payload_update:
            self._ensure_runtime_lease()
            self.basic.set_payload(self.payload_mass_kg, self.payload_com_tool_m.tolist())
        return self.runtime_settings_snapshot()

    def runtime_settings_snapshot(self) -> dict[str, Any]:
        with self.lock:
            gains = self._gain_profile_locked() if self.basic_model else []
            return {
                "execution_mode": self.execution_mode,
                "interaction_mode": self.interaction_mode,
                "ik_mode": self.ik_mode,
                "duration_s": self.duration_s,
                "replan_interval_s": self.replan_interval_s,
                "kp_multiplier": self.kp_multiplier,
                "controlled_frame_offset_xyz_m": self.tool_offset_xyz_m.tolist(),
                "controlled_frame_offset_rpy_rad": self.tool_offset_rpy_rad.tolist(),
                "payload_mass_kg": self.payload_mass_kg,
                "payload_com_tool_m": self.payload_com_tool_m.tolist(),
                "contact_torque_budget_nm": copy.deepcopy(
                    self.config["contact"]["task_torque_budget_nm"]
                ),
                "contact_budget_mode": str(self.config["contact"]["budget_mode"]).upper(),
                "contact_wrench_force_budget_n": copy.deepcopy(
                    self.config["contact"]["wrench_force_budget_n"]
                ),
                "contact_wrench_torque_budget_nm": copy.deepcopy(
                    self.config["contact"]["wrench_torque_budget_nm"]
                ),
                "contact_isotropic_force_budget_n": self.config["contact"]["isotropic_force_budget_n"],
                "contact_isotropic_torque_budget_nm": self.config["contact"]["isotropic_torque_budget_nm"],
                "effective_gains": gains,
                "gripper": self._gripper_settings_snapshot_locked(),
            }

    def _gripper_settings_snapshot_locked(self) -> dict[str, Any]:
        return {
            "mode": self.gripper_mode,
            "basic_mode": MODE_MIT if self.gripper_mode == GRIPPER_MIT else "POSITION_EFFORT_LIMITED",
            "open_position_rad": self.gripper_open_position_rad,
            "closed_position_rad": self.gripper_closed_position_rad,
            "velocity_limit_rad_s": self.gripper_velocity_limit_rad_s,
            "torque_limit_ratio": self.gripper_torque_limit_ratio,
            "mit_kp": self.gripper_mit_kp,
            "mit_kd": self.gripper_mit_kd,
            "keepalive_hz": self.gripper_keepalive_hz,
        }

    def set_gripper_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            if self.trajectory is not None or self.gripper_active_action is not None:
                raise RuntimeError("gripper settings cannot change during active motion")
            original = self._gripper_settings_snapshot_locked()
            try:
                if "mode" in payload:
                    self.gripper_mode = str(payload["mode"]).strip().upper()
                if "open_position_rad" in payload:
                    self.gripper_open_position_rad = float(payload["open_position_rad"])
                if "closed_position_rad" in payload:
                    self.gripper_closed_position_rad = float(payload["closed_position_rad"])
                if "velocity_limit_rad_s" in payload:
                    self.gripper_velocity_limit_rad_s = float(payload["velocity_limit_rad_s"])
                if "torque_limit_ratio" in payload:
                    self.gripper_torque_limit_ratio = float(payload["torque_limit_ratio"])
                if "mit_kp" in payload:
                    self.gripper_mit_kp = float(payload["mit_kp"])
                if "mit_kd" in payload:
                    self.gripper_mit_kd = float(payload["mit_kd"])
                self._validate_runtime_values()
                self._validate_gripper_against_model_locked()
            except Exception:
                self.gripper_mode = str(original["mode"])
                self.gripper_open_position_rad = float(original["open_position_rad"])
                self.gripper_closed_position_rad = float(original["closed_position_rad"])
                self.gripper_velocity_limit_rad_s = float(original["velocity_limit_rad_s"])
                self.gripper_torque_limit_ratio = float(original["torque_limit_ratio"])
                self.gripper_mit_kp = float(original["mit_kp"])
                self.gripper_mit_kd = float(original["mit_kd"])
                raise
            return self._gripper_settings_snapshot_locked()

    def stage_external_command(
        self,
        payload: dict[str, Any],
        *,
        source: str = "fabric",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Stage a Cartesian command without granting physical motion authority."""
        if not isinstance(payload, dict):
            raise ValueError("external command data must be an object")
        command_type = str(payload.get("command_type", "CARTESIAN_TARGET")).strip().upper()
        if command_type != "CARTESIAN_TARGET":
            raise ValueError(f"unsupported external command_type {command_type}")

        settings_payload = payload.get("settings", {})
        if settings_payload is None:
            settings_payload = {}
        if not isinstance(settings_payload, dict):
            raise ValueError("external command settings must be an object")
        settings = dict(settings_payload)
        ik_offset_payload = payload.get("ik_offset")
        if ik_offset_payload is not None:
            if not isinstance(ik_offset_payload, dict):
                raise ValueError("ik_offset must be an object")
            if "xyz_m" in ik_offset_payload:
                settings.setdefault(
                    "controlled_frame_offset_xyz_m", ik_offset_payload["xyz_m"]
                )
            if "rpy_rad" in ik_offset_payload:
                settings.setdefault(
                    "controlled_frame_offset_rpy_rad", ik_offset_payload["rpy_rad"]
                )
        if "ik_offset_xyz_m" in payload:
            settings.setdefault(
                "controlled_frame_offset_xyz_m", payload["ik_offset_xyz_m"]
            )
        if "ik_offset_rpy_rad" in payload:
            settings.setdefault(
                "controlled_frame_offset_rpy_rad", payload["ik_offset_rpy_rad"]
            )
        for key in (
            "execution_mode",
            "interaction_mode",
            "ik_mode",
            "duration_s",
            "replan_interval_s",
            "kp_multiplier",
            "controlled_frame_offset_xyz_m",
            "controlled_frame_offset_rpy_rad",
            "payload_mass_kg",
            "payload_com_tool_m",
            "contact_torque_budget_nm",
            "task_torque_budget_nm",
            "contact_budget_mode",
            "contact_wrench_force_budget_n",
            "contact_wrench_torque_budget_nm",
            "contact_isotropic_force_budget_n",
            "contact_isotropic_torque_budget_nm",
        ):
            if key in payload and key not in settings:
                settings[key] = payload[key]

        if settings:
            with self.lock:
                active = self.trajectory is not None
                current = self.runtime_settings_snapshot()
            if active:
                changed = [key for key, value in settings.items() if key in current and value != current[key]]
                if changed:
                    raise RuntimeError(
                        "external runtime settings cannot change during active motion: " + ", ".join(changed)
                    )
            else:
                self.set_runtime_settings(settings)

        target_payload = payload.get(
            "ik_location",
            payload.get("target", payload.get("cartesian_target")),
        )
        if target_payload is None:
            raise ValueError(
                "external command requires ik_location, target, or cartesian_target"
            )
        if not isinstance(target_payload, dict):
            raise ValueError("external Cartesian target must be an object")
        if "position_m" not in target_payload:
            raise ValueError("external Cartesian target requires position_m")
        position = np.asarray(target_payload["position_m"], dtype=float)
        if position.shape != (3,) or not np.all(np.isfinite(position)):
            raise ValueError("external target position_m must contain three finite values")
        orientation = target_payload.get("rpy_rad")
        rpy = None if orientation is None else np.asarray(orientation, dtype=float)
        if rpy is not None and (rpy.shape != (3,) or not np.all(np.isfinite(rpy))):
            raise ValueError("external target rpy_rad must contain three finite values")
        gravity_payload = payload.get("ik_gravity_offset", {})
        if gravity_payload is None:
            gravity_payload = {}
        if not isinstance(gravity_payload, dict):
            raise ValueError("ik_gravity_offset must be an object")
        gravity_xyz = np.asarray(
            gravity_payload.get(
                "xyz_m", payload.get("ik_gravity_offset_xyz_m", [0.0, 0.0, 0.0])
            ),
            dtype=float,
        )
        gravity_rpy = np.asarray(
            gravity_payload.get(
                "rpy_rad",
                payload.get("ik_gravity_offset_rpy_rad", [0.0, 0.0, 0.0]),
            ),
            dtype=float,
        )
        if gravity_xyz.shape != (3,) or not np.all(np.isfinite(gravity_xyz)):
            raise ValueError("ik_gravity_offset xyz_m must contain three finite values")
        if gravity_rpy.shape != (3,) or not np.all(np.isfinite(gravity_rpy)):
            raise ValueError("ik_gravity_offset rpy_rad must contain three finite values")
        corrected_position = position + gravity_xyz

        now = time.monotonic()
        with self.lock:
            if self.staged_target is None:
                raise RuntimeError("staged target is unavailable until the controller is HOT")
            self._assert_workspace_target_locked(corrected_position)
            candidate = self.staged_target.copy()
            candidate[:3, 3] = corrected_position
            if rpy is not None and self.ik_mode == IK_POSE_6DOF:
                candidate[:3, :3] = rpy_matrix(rpy)
            if self.ik_mode == IK_POSE_6DOF and np.any(np.abs(gravity_rpy) > 0.0):
                candidate[:3, :3] = rpy_matrix(gravity_rpy) @ candidate[:3, :3]
            self.staged_target = candidate
            self.last_target_update = now
            self._invalidate_preview_locked()
            self.external_target_update_count += 1
            self.last_external_target_source = str(source)
            self.last_external_target_monotonic = now
            self.last_external_target_metadata = {
                **copy.deepcopy(metadata or {}),
                "ik_components": {
                    "location": {
                        "position_m": position.tolist(),
                        "rpy_rad": None if rpy is None else rpy.tolist(),
                    },
                    "tool_to_acting_point_offset": {
                        "xyz_m": self.tool_offset_xyz_m.tolist(),
                        "rpy_rad": self.tool_offset_rpy_rad.tolist(),
                    },
                    "base_frame_gravity_offset": {
                        "xyz_m": gravity_xyz.tolist(),
                        "rpy_rad": gravity_rpy.tolist(),
                    },
                    "corrected_position_m": corrected_position.tolist(),
                },
            }
            return {
                "accepted": True,
                "physical_motion_authorized": False,
                "external_target_update_count": self.external_target_update_count,
                "staged_target": self._frame_payload(self.staged_target),
                "runtime": self.runtime_settings_snapshot(),
            }

    def _invalidate_preview_locked(self) -> None:
        self.target_revision += 1
        self.last_preview = None
        self.last_preview_context = None

    def stage_scene(
        self,
        payload: dict[str, Any],
        *,
        source: str = "fabric",
    ) -> dict[str, Any]:
        scene = SceneSnapshot.from_payload(
            payload,
            maximum_spheres=int(self.config["scene_input"]["maximum_spheres"]),
        )
        with self.lock:
            self.scene = scene
            self.scene_source = str(source)
            self.scene_received_monotonic = time.monotonic()
            self._invalidate_preview_locked()
            return {
                "accepted": True,
                "physical_motion_authorized": False,
                "scene": scene.snapshot(),
            }

    def preview_staged_target(
        self,
        *,
        allowed_contact_object_ids: set[str] | None = None,
        permit_pushable_contact: bool = False,
    ) -> dict[str, Any]:
        """Solve and sample a candidate plan without engaging or sending a command."""
        try:
            state = self.basic.state()
            with self.lock:
                self.basic_state = state
                self.last_state_success = time.monotonic()
                if self.residency != "HOT" or self.kinematics is None or self.staged_target is None:
                    raise RuntimeError("controller must be HOT before preview")
                q_start = self._measured_positions_locked()[:6].copy()
                origin = self.kinematics.controlled_frame(q_start, self._tool_to_control_locked())
                requested, clamped = self._clamped_target_locked(origin, self.staged_target)
                continuity = solve_cartesian_continuity(
                    q_start,
                    origin,
                    requested,
                    self._solve_target_locked,
                    waypoint_count=int(self.config["planning"]["cartesian_waypoint_count"]),
                )
                q_goal = continuity.q_waypoints[-1]
                duration = self._duration_for_move_locked(q_start, q_goal, self.duration_s)
                preview = build_waypoint_preview(
                    self.kinematics,
                    continuity.q_waypoints,
                    duration,
                    scene=self.scene,
                    link_radii_m=self.config["trajectory"]["link_radii_m"],
                    sample_count=int(self.config["trajectory"]["preview_sample_count"]),
                    allowed_contact_object_ids=set(allowed_contact_object_ids or set()),
                    permit_pushable_contact=bool(permit_pushable_contact),
                )
                planning_reasons: list[str] = []
                scene_required = self.execution_mode in {
                    str(value).upper() for value in self.config["safety"]["scene_required_execution_modes"]
                }
                if scene_required and self.scene is None:
                    planning_reasons.append("a fresh semantic scene is required for this execution mode")
                if scene_required and self.scene_received_monotonic:
                    scene_age_ms = (time.monotonic() - self.scene_received_monotonic) * 1000.0
                    if scene_age_ms > float(self.config["scene_input"]["max_age_ms"]):
                        planning_reasons.append(f"semantic scene is stale ({scene_age_ms:.0f} ms)")
                if not preview.collision_free:
                    planning_reasons.append("candidate path intersects a non-permitted semantic object")
                planning_config = self.config["planning"]
                if continuity.minimum_sigma <= 0.0 or continuity.minimum_sigma < float(planning_config["minimum_jacobian_sigma"]):
                    planning_reasons.append(
                        f"Cartesian waypoint path approaches a singularity (sigma {continuity.minimum_sigma:.5f})"
                    )
                if continuity.maximum_waypoint_joint_step_rad > float(planning_config["maximum_waypoint_joint_step_rad"]):
                    planning_reasons.append("IK continuity path contains a large joint jump")
                endpoint_limits = np.asarray(planning_config["maximum_endpoint_joint_delta_rad"], dtype=float)
                endpoint_delta = np.abs(continuity.q_waypoints[-1] - continuity.q_waypoints[0])
                if np.any(endpoint_delta > endpoint_limits):
                    joints = ", ".join(str(index + 1) for index in np.flatnonzero(endpoint_delta > endpoint_limits))
                    planning_reasons.append(f"IK endpoint requires excessive joint travel on joints {joints}")
                if continuity.total_joint_travel_rad > float(planning_config["maximum_total_joint_travel_rad"]):
                    planning_reasons.append("IK path has excessive aggregate joint travel")
                if self.execution_mode == "CONTACT_WORK" and self.ik_mode != IK_POSE_6DOF:
                    planning_reasons.append("CONTACT_WORK requires POSE_6DOF")
                contact_distance = float(np.linalg.norm(requested[:3, 3] - origin[:3, 3]))
                if self.execution_mode == "CONTACT_WORK" and contact_distance > float(self.config["contact"]["maximum_translation_m"]):
                    planning_reasons.append("CONTACT_WORK exceeds its configured short-stroke distance")
                if self.execution_mode == "CONTACT_WORK" and self.torque_baseline is not None:
                    baseline_frame = self.kinematics.controlled_frame(
                        self.torque_baseline.positions_rad,
                        self._tool_to_control_locked(),
                    )
                    baseline_distance = float(np.linalg.norm(origin[:3, 3] - baseline_frame[:3, 3]))
                    if baseline_distance > float(self.config["contact"]["maximum_translation_m"]):
                        planning_reasons.append("current pose is too far from the captured torque baseline")
                preview_is_required = self.execution_mode in {
                    str(value).upper() for value in self.config["safety"]["preview_required_execution_modes"]
                }
                physical_reasons = list(planning_reasons) if preview_is_required else []
                enabled = {
                    str(value).upper() for value in self.config["safety"]["physically_enabled_execution_modes"]
                }
                if self.execution_mode not in enabled:
                    physical_reasons.append(f"{self.execution_mode} is preview-only in the current configuration")
                if self.execution_mode == "CONTACT_WORK" and not self._contact_budget_configured_locked():
                    physical_reasons.append(
                        "CONTACT_WORK requires a complete JOINT_6, WRENCH_6, or ISOTROPIC_2 budget"
                    )
                if self.execution_mode == "CONTACT_WORK" and self.torque_baseline is None:
                    physical_reasons.append("CONTACT_WORK requires an operator-captured steady torque baseline")
                self.last_preview = preview
                self.last_preview_context = {
                    "target_revision": self.target_revision,
                    "execution_mode": self.execution_mode,
                    "ik_mode": self.ik_mode,
                    "target_clamped": clamped,
                    "position_residual_m": continuity.final_position_residual_m,
                    "orientation_residual_rad": continuity.final_orientation_residual_rad,
                    "cartesian_continuity": continuity.snapshot(),
                    "planning_valid": not planning_reasons,
                    "planning_reasons": planning_reasons,
                    "physical_execution_enabled": not physical_reasons,
                    "physical_execution_blockers": physical_reasons,
                }
                self.preview_count += 1
                self.last_position_residual_m = continuity.final_position_residual_m
                self.last_orientation_residual_rad = continuity.final_orientation_residual_rad
                return {
                    **preview.snapshot(include_samples=False),
                    **copy.deepcopy(self.last_preview_context),
                    "physical_motion_authorized": False,
                }
        except Exception:
            with self.lock:
                self.preview_rejected_count += 1
            raise

    def capture_contact_baseline(self) -> dict[str, Any]:
        """Capture steady torque while floating; this operation never sends a motion target."""
        return self._capture_contact_baseline()

    def _capture_contact_baseline(self) -> dict[str, Any]:
        self.request_float()
        duration = float(self.config["contact"]["baseline_duration_s"])
        minimum_samples = int(self.config["contact"]["baseline_minimum_samples"])
        interval = duration / max(minimum_samples - 1, 1)
        samples: list[dict[str, Any]] = []
        with self.lock:
            self.baseline_capture_state = "CAPTURING_IN_GRAVITY_FLOAT"
            self.baseline_capture_error = None
        try:
            deadline = time.monotonic() + duration
            while len(samples) < minimum_samples or time.monotonic() < deadline:
                state = self.basic.state()
                provider_state = str(state.get("provider_state") or state.get("state") or "")
                if provider_state != "SAFE_HOLD_GRAVITY_FLOAT":
                    raise RuntimeError(f"baseline capture requires SAFE_HOLD_GRAVITY_FLOAT, got {provider_state or 'unknown'}")
                samples.append(state)
                if len(samples) >= minimum_samples and time.monotonic() >= deadline:
                    break
                time.sleep(max(0.005, interval))
            baseline = TorqueBaseline.from_samples(
                samples,
                maximum_velocity_rad_s=self.config["contact"]["baseline_maximum_velocity_rad_s"],
                maximum_mad_nm=self.config["contact"]["baseline_maximum_mad_nm"],
            )
            with self.lock:
                self.torque_baseline = baseline
                self.baseline_capture_state = "CAPTURED"
                self.baseline_captured_monotonic = time.monotonic()
                self.contact_torque_limit_ratios = None
                self.contact_effective_joint_budget_nm = None
                self.contact_residual_nm = None
                self.contact_limit_violations = []
                self.contact_saturated_joint_indices = []
                self.contact_saturation_reason = None
                self.basic_state = copy.deepcopy(samples[-1])
                self.last_state_success = self.baseline_captured_monotonic
                return {
                    "captured": True,
                    "physical_motion_authorized": False,
                    "automatic": False,
                    "baseline": baseline.snapshot(),
                }
        except Exception as exc:
            with self.lock:
                self.torque_baseline = None
                self.baseline_capture_state = "REJECTED"
                self.baseline_capture_error = str(exc)
                self.contact_torque_limit_ratios = None
                self.contact_effective_joint_budget_nm = None
                self.contact_residual_nm = None
                self.contact_limit_violations = []
            raise

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self, hot: bool = True) -> None:
        if self.control_thread and self.control_thread.is_alive():
            return
        self.stop_event.clear()
        if hot:
            self.enter_hot()
        self.control_thread = threading.Thread(target=self._control_loop, name="arm-mit-control", daemon=True)
        self.lease_thread = threading.Thread(target=self._lease_loop, name="arm-mit-lease", daemon=True)
        self.control_thread.start()
        self.lease_thread.start()

    def enter_hot(self) -> dict[str, Any]:
        with self.lock:
            if self.residency == "HOT" and self.basic.lease_snapshot() is not None:
                return {"status": "already_hot_float"}
        model = self.basic.model()
        state = self.basic.state()
        lease = self.basic.acquire(
            f"{self.config['provider_id']}:{self.config['arm_id']}",
            int(self.config["lease_duration_ms"]),
        )
        positions = self._positions_from_state(state)
        kinematics = ArmKinematics(model)
        with self.lock:
            self.basic_model = model
            self.basic_state = state
            self.kinematics = kinematics
            self.last_state_success = time.monotonic()
            self.residency = "HOT"
            self.ready = True
            self.health = "HEALTHY"
            self.control_state = "TARGET_EDIT"
            self.engaged = False
            self.input = InputState()
            self._lb_previous = False
            self.commit_requested = False
            self.replan_requested = False
            self.fault_reason = None
            self.last_error = None
            self.staged_target = kinematics.controlled_frame(positions[:6], self._tool_to_control_locked())
            self.last_target_update = time.monotonic()
            self._invalidate_preview_locked()
            self.goal_q = positions[:6].copy()
            self.commanded_q = positions[:6].copy()
            self.commanded_qd = np.zeros(6, dtype=float)
            self.trajectory = None
            self.latched_endpoint = None
            self.hybrid_policy = None
            self.gripper_gamepad_action = None
            self.gripper_ui_action = None
            self.gripper_active_action = None
            self.gripper_target_rad = None
            self.gripper_fault_latched = False
            self._validate_gripper_against_model_locked()
            self.lease_state = "OWNED"
            self.last_lease_attempt = time.monotonic()
            self.last_lease_renew = self.last_lease_attempt
        self.basic.set_payload(self.payload_mass_kg, self.payload_com_tool_m.tolist())
        if not self._verify_float_once("MIT controller HOT idle"):
            try:
                self.basic.release("MIT startup float verification failed")
            finally:
                with self.lock:
                    self.lease_state = "LOST"
            raise RuntimeError("Basic Controller gravity-float could not be verified")
        print(f"[mit-lease] acquired generation={lease.fencing_generation} holder={lease.holder}")
        return {"status": "hot_target_edit"}

    def enter_warm(self) -> dict[str, Any]:
        float_ok=self._cancel_active_trajectory("controller WARM", request_float=True)
        if self.basic.lease_snapshot() is not None and not float_ok:
            raise RuntimeError(
                "Integrated WARM refused to release the Basic lease because completed gravity-float was not confirmed"
            )
        with self.lock:
            self.residency = "WARM"
            self.ready = False
            self.engaged = False
            self.control_state = "IDLE_FLOAT"
            self.commit_requested = False
            self.replan_requested = False
            self.gripper_gamepad_action = None
            self.gripper_ui_action = None
            self.gripper_active_action = None
            self.gripper_target_rad = None
            self.gripper_fault_latched = False
        if self.basic.lease_snapshot() is not None:
            self.basic.release("MIT controller WARM release")
        with self.lock:
            self.lease_state = "NONE"
        return {"status": "warm"}

    def stop(self) -> None:
        if self.stop_event.is_set():
            return
        self.stop_event.set()
        self._cancel_active_trajectory("MIT controller termination", request_float=False)
        with self.lock:
            self.residency = "COLD"
            self.ready = False
            self.engaged = False
        if self.basic.lease_snapshot() is not None:
            try:
                self.basic.release("MIT controller termination release")
            except LeaseLostError:
                pass
        with self.lock:
            self.lease_state = "NONE"
            self.control_state = "STOPPING"

    # ------------------------------------------------------------------
    # Platform safety and operator interface
    # ------------------------------------------------------------------
    def update_platform_status(
        self,
        manager_registered: bool,
        fabric_ready: bool,
        errors: dict[str, str | None] | None = None,
        *,
        motion_inhibited: bool = False,
        motion_inhibit_owners: list[dict[str, Any]] | None = None,
    ) -> None:
        should_float = False
        with self.lock:
            self.manager_registered = bool(manager_registered)
            self.fabric_ready = bool(fabric_ready)
            self.motion_inhibited = bool(motion_inhibited)
            self.motion_inhibit_owners = list(motion_inhibit_owners or [])
            if errors is not None:
                self.platform_errors = dict(errors)
            if self.trajectory is not None and not self._platform_ready_locked():
                self._set_fault_locked("Manager, Fabric, or motion authority became unavailable")
                should_float = True
        if should_float:
            self._cancel_active_trajectory("platform availability or motion authority lost", request_float=True)

    def set_engaged(self, enabled: bool) -> dict[str, Any]:
        if not bool(enabled):
            self._cancel_active_trajectory("operator disengaged", request_float=True)
            with self.lock:
                self.engaged = False
                self.gripper_gamepad_action = None
                self.gripper_ui_action = None
                self.gripper_active_action = None
                self.gripper_target_rad = None
                self.gripper_fault_latched = False
                self.control_state = "TARGET_EDIT" if self.residency == "HOT" else "IDLE_FLOAT"
            return {"status": "disengaged_float", "engaged": False}
        self._ensure_runtime_lease()
        if not self._request_float_once("physical staging engagement"):
            raise RuntimeError("Basic Controller gravity-float could not be verified")
        with self.lock:
            if self.residency != "HOT":
                raise RuntimeError("controller must be HOT")
            self._assert_motion_prerequisites_locked(time.monotonic())
            self.engaged = True
            self.control_state = "TARGET_EDIT"
            self.fault_reason = None
            self.last_error = None
            self.health = "HEALTHY"
        return {"status": "engaged_target_edit", "engaged": True}

    def update_input(self, payload: dict[str, Any]) -> None:
        now = time.monotonic()
        deadzone = float(self.config["teleop"]["deadzone"])

        def axis(name: str) -> float:
            value = float(payload.get(name, 0.0))
            if not np.isfinite(value):
                return 0.0
            value = float(np.clip(value, -1.0, 1.0))
            return 0.0 if abs(value) < deadzone else value

        lb = bool(payload.get("lb", payload.get("deadman", False)))
        rb_open = bool(payload.get("gripper_open", payload.get("rb", False)))
        rt_close = bool(payload.get("gripper_close", payload.get("rt", False)))
        cancel_hold = False
        with self.lock:
            rising = lb and not self._lb_previous
            falling = (not lb) and self._lb_previous
            self._lb_previous = lb
            self.input = InputState(
                received_monotonic=now,
                x=axis("x"), y=axis("y"), z=axis("z"),
                roll=axis("roll"), pitch=axis("pitch"), yaw=axis("yaw"),
                lb_pressed=lb,
                rb_open_pressed=rb_open,
                rt_close_pressed=rt_close,
            )
            self.gripper_gamepad_action = (
                GRIPPER_OPEN if rb_open and not rt_close
                else GRIPPER_CLOSE if rt_close and not rb_open
                else None
            )
            if self.engaged and rising and self.trajectory is None:
                self.commit_requested = True
            if falling and self.trajectory is not None and self.trajectory.interaction_mode == INTERACTION_HOLD_LB:
                cancel_hold = True
        if cancel_hold:
            self._cancel_active_trajectory("LB released in hold mode", request_float=True)

    def request_gripper(self, action: str) -> dict[str, Any]:
        normalized = str(action).strip().upper()
        if normalized not in SUPPORTED_GRIPPER_ACTIONS | {"STOP"}:
            raise ValueError("gripper action must be OPEN, CLOSE, or STOP")
        with self.lock:
            if normalized != "STOP" and not self.engaged:
                raise PermissionError("Engage physical control before operating the gripper")
            if normalized != "STOP" and self.trajectory is not None:
                raise RuntimeError("gripper is blocked while an arm trajectory is active")
            self.gripper_ui_action = None if normalized == "STOP" else normalized
            desired = self._desired_gripper_action_locked()
            return {
                "accepted": True,
                "requested_action": desired,
                "hold_to_run": False,
                "release_behavior": "LATCH_LAST_ENDPOINT",
                "requires_engage": True,
            }

    def request_float(self) -> dict[str, Any]:
        self._cancel_active_trajectory("operator float", request_float=True)
        with self.lock:
            self.engaged = False
            self.commit_requested = False
            self.replan_requested = False
            self.gripper_gamepad_action = None
            self.gripper_ui_action = None
            self.gripper_active_action = None
            self.gripper_target_rad = None
            self.gripper_fault_latched = False
            self.control_state = "TARGET_EDIT" if self.residency == "HOT" else "IDLE_FLOAT"
        return {"status": "gravity_float", "engaged": False}

    # ------------------------------------------------------------------
    # Target editing and planning
    # ------------------------------------------------------------------
    def _control_loop(self) -> None:
        period = 1.0 / max(float(self.config["control_rate_hz"]), 1.0)
        next_tick = time.monotonic()
        while not self.stop_event.is_set():
            now = time.monotonic()
            if now < next_tick:
                self.stop_event.wait(next_tick - now)
                continue
            if now - next_tick > period:
                next_tick += int((now - next_tick) // period) * period
            next_tick += period
            try:
                self._tick()
            except Exception as exc:
                self._fault_to_float(f"controller exception: {exc}")

    def _tick(self) -> None:
        now = time.monotonic()
        poll_period = 1.0 / max(float(self.config["basic_state_rate_hz"]), 1.0)
        if now - self.last_state_poll >= poll_period:
            self.last_state_poll = now
            try:
                state = self.basic.state()
                with self.lock:
                    self.basic_state = state
                    self.last_state_success = now
            except Exception as exc:
                with self.lock:
                    self.last_error = f"Basic state poll failed: {exc}"
                    self.health = "DEGRADED"

        commit = False
        replan = False
        with self.lock:
            if self.residency != "HOT" or not self.ready:
                return
            if self.trajectory is None or self.trajectory.interaction_mode == INTERACTION_HOLD_LB:
                self._integrate_staged_target_locked(now)
            if self.commit_requested:
                self.commit_requested = False
                commit = True
            plan = self.trajectory
            if (
                plan is not None
                and plan.interaction_mode == INTERACTION_HOLD_LB
                and plan.execution_mode != CONTACT_WORK
                and self.input.lb_pressed
                and self.target_revision != plan.target_revision
                and now - plan.last_replan_monotonic >= self.replan_interval_s
            ):
                plan.last_replan_monotonic = now
                replan = True
        self._service_gripper(now)
        if commit:
            self._commit_staged_target()
        elif replan:
            self._replan_continuous()

    def _integrate_staged_target_locked(self, now: float) -> None:
        if self.staged_target is None:
            return
        input_age_ms = (now - self.input.received_monotonic) * 1000.0 if self.input.received_monotonic else float("inf")
        if input_age_ms > float(self.config["input_timeout_ms"]):
            self.last_target_update = now
            return
        previous = self.last_target_update or now
        dt = min(max(now - previous, 0.0), float(self.config["teleop"]["maximum_integration_dt_s"]))
        self.last_target_update = now
        translation_rate = float(self.config["teleop"]["translation_rate_m_s"])
        delta = np.array([self.input.x, self.input.y, self.input.z], dtype=float) * translation_rate * dt
        candidate = self.staged_target.copy()
        candidate[:3, 3] += delta
        workspace = self.config["workspace"]
        candidate[0, 3] = float(np.clip(candidate[0, 3], -float(workspace["abs_x_max_m"]), float(workspace["abs_x_max_m"])))
        candidate[1, 3] = float(np.clip(candidate[1, 3], -float(workspace["abs_y_max_m"]), float(workspace["abs_y_max_m"])))
        candidate[2, 3] = float(np.clip(candidate[2, 3], float(workspace["z_min_m"]), float(workspace["z_max_m"])))
        if self.ik_mode == IK_POSE_6DOF:
            rotation_rate = float(self.config["teleop"]["rotation_rate_rad_s"])
            incremental = np.array([self.input.roll, self.input.pitch, self.input.yaw], dtype=float) * rotation_rate * dt
            if np.any(np.abs(incremental) > 0.0):
                candidate[:3, :3] = rpy_matrix(incremental) @ candidate[:3, :3]
        changed = not np.allclose(candidate, self.staged_target, rtol=0.0, atol=1e-12)
        self.staged_target = candidate
        if changed:
            self._invalidate_preview_locked()

    def _clamped_target_locked(self, origin: np.ndarray, requested: np.ndarray) -> tuple[np.ndarray, bool]:
        output = requested.copy()
        displacement = output[:3, 3] - origin[:3, 3]
        distance = float(np.linalg.norm(displacement))
        maximum = float(self.config["trajectory"]["maximum_translation_per_commit_m"])
        clamped = False
        if distance > maximum and distance > 1e-12:
            output[:3, 3] = origin[:3, 3] + displacement * (maximum / distance)
            clamped = True
        self._assert_workspace_target_locked(output[:3, 3])
        return output, clamped

    def _solve_target_locked(self, q_seed: np.ndarray, target: np.ndarray):
        if self.kinematics is None:
            raise RuntimeError("kinematics unavailable")
        pose_mode = self.ik_mode == IK_POSE_6DOF
        result = self.kinematics.solve_weighted_pose(
            q_seed,
            target,
            position_tolerance_m=float(self.config["ik"]["position_tolerance_m"]),
            orientation_tolerance_rad=float(self.config["ik"]["orientation_tolerance_rad"]),
            maximum_iterations=int(self.config["ik"]["maximum_iterations"]),
            damping=float(self.config["ik"]["damping"]),
            maximum_step_rad=float(self.config["ik"]["maximum_iteration_step_rad"]),
            joint_margin_rad=float(self.config["ik"]["joint_margin_rad"]),
            orientation_weight_m_per_rad=float(self.config["ik"]["orientation_weight_m_per_rad"]) if pose_mode else 0.0,
            orientation_required=pose_mode,
            tool_to_control=self._tool_to_control_locked(),
        )
        return result

    def _duration_for_move_locked(self, q_start: np.ndarray, q_goal: np.ndarray, requested: float) -> float:
        caps = (
            self._provider_pos_vel_caps_locked()
            if self.execution_mode == TRANSIT_SPEED
            else self._provider_rate_caps_locked()
        )
        required = float(np.max(1.5 * np.abs(q_goal - q_start) / np.maximum(caps, 1e-6)))
        duration = max(float(requested), required)
        maximum = float(self.config["runtime_limits"]["duration_max_s"])
        if duration > maximum:
            if self.execution_mode in {TRANSIT_SPEED, CONTACT_WORK}:
                return maximum
            raise RuntimeError(f"trajectory requires {duration:.2f} s for provider rate caps; maximum is {maximum:.2f} s")
        return duration

    def _continuous_horizon_locked(self, q_start: np.ndarray, q_goal: np.ndarray) -> float:
        requested = max(float(self.config["trajectory"]["continuous_horizon_min_s"]), 4.0 * self.replan_interval_s)
        requested = min(requested, self.duration_s)
        return self._duration_for_move_locked(q_start, q_goal, requested)

    def _commit_staged_target(self) -> None:
        try:
            state = self.basic.state()
            now = time.monotonic()
            with self.lock:
                self.basic_state = state
                self.last_state_success = now
                self._assert_motion_prerequisites_locked(now)
                if not self.engaged:
                    raise PermissionError("click Engage physical control before committing a target")
                if self.trajectory is not None:
                    raise RuntimeError("a trajectory is already active")
                if self._desired_gripper_action_locked() is not None:
                    raise RuntimeError("release RB/RT before committing an arm trajectory; the latched gripper hold is preserved")
                if self.kinematics is None or self.staged_target is None:
                    raise RuntimeError("kinematics or staged target is unavailable")
                enabled_modes = {
                    str(value).upper() for value in self.config["safety"]["physically_enabled_execution_modes"]
                }
                if self.execution_mode not in enabled_modes:
                    raise PermissionError(f"{self.execution_mode} is preview-only and cannot send physical commands")
                preview_required = self.execution_mode in {
                    str(value).upper() for value in self.config["safety"]["preview_required_execution_modes"]
                }
                preview_context = self.last_preview_context or {}
                if preview_required and (
                    self.last_preview is None
                    or preview_context.get("target_revision") != self.target_revision
                    or preview_context.get("execution_mode") != self.execution_mode
                    or preview_context.get("ik_mode") != self.ik_mode
                    or not preview_context.get("planning_valid", False)
                ):
                    raise PermissionError("a current valid non-motion preview is required before physical commit")
                if self.interaction_mode == INTERACTION_HOLD_LB and not self.input.lb_pressed:
                    return
                q_start = self._measured_positions_locked()[:6].copy()
                origin = self.kinematics.controlled_frame(q_start, self._tool_to_control_locked())
                requested, clamped = self._clamped_target_locked(origin, self.staged_target)
                if clamped:
                    self.staged_target = requested.copy()
                distance = float(np.linalg.norm(requested[:3, 3] - origin[:3, 3]))
                orientation_delta = float(np.linalg.norm(rotation_vector(requested[:3, :3] @ origin[:3, :3].T)))
                if self.execution_mode == CONTACT_WORK:
                    if self.ik_mode != IK_POSE_6DOF:
                        raise PermissionError("CONTACT_WORK requires POSE_6DOF")
                    if self.torque_baseline is None:
                        raise PermissionError(
                            "capture a steady gravity-float torque baseline before CONTACT_WORK"
                        )
                    if not self._contact_budget_configured_locked():
                        raise PermissionError(
                            "configure a complete JOINT_6, WRENCH_6, or ISOTROPIC_2 budget before CONTACT_WORK"
                        )
                    maximum_contact = float(self.config["contact"]["maximum_translation_m"])
                    if distance > maximum_contact:
                        raise PlanningRejected(
                            f"CONTACT_WORK translation {distance:.4f} m exceeds {maximum_contact:.4f} m"
                        )
                    baseline_frame = self.kinematics.controlled_frame(
                        self.torque_baseline.positions_rad,
                        self._tool_to_control_locked(),
                    )
                    baseline_distance = float(
                        np.linalg.norm(origin[:3, 3] - baseline_frame[:3, 3])
                    )
                    if baseline_distance > maximum_contact:
                        raise PlanningRejected(
                            "current arm pose is too far from the captured contact baseline"
                        )
                minimum = float(self.config["trajectory"].get("minimum_translation_per_commit_m", 0.0005))
                if distance < minimum and (self.ik_mode == IK_POSITION_3DOF or orientation_delta < 1e-4):
                    self.last_commit_origin = origin.copy()
                    self.last_committed_target = requested.copy()
                    self.last_target_clamped = clamped
                    self.last_position_residual_m = 0.0
                    self.last_orientation_residual_rad = 0.0
                    self.last_ik_iterations = 0
                    self.last_sigma_min = None
                    self.commit_count += 1
                    self.last_error = "target is already at the current controlled frame; no trajectory was sent"
                    return
                result = self._solve_target_locked(q_start, requested)
                q_goal = result.q_goal.copy()
                duration = (
                    self._continuous_horizon_locked(q_start, q_goal)
                    if self.interaction_mode == INTERACTION_HOLD_LB
                    else self._duration_for_move_locked(q_start, q_goal, self.duration_s)
                )
                self.last_commit_origin = origin.copy()
                self.last_committed_target = requested.copy()
                self.last_target_clamped = clamped
                self.last_position_residual_m = float(result.position_residual_m)
                self.last_orientation_residual_rad = float(result.orientation_residual_rad)
                self.last_ik_iterations = int(result.iterations)
                self.last_sigma_min = float(result.sigma_min)
                self.goal_q = q_goal.copy()
                self.commanded_q = q_start.copy()
                self.commanded_qd = np.zeros(6, dtype=float)
                self.commit_count += 1
                self.trajectory = TrajectoryPlan(
                    execution_mode=self.execution_mode,
                    interaction_mode=self.interaction_mode,
                    ik_mode=self.ik_mode,
                    q_start=q_start,
                    qd_start=np.zeros(6, dtype=float),
                    q_goal=q_goal,
                    segment_started_monotonic=time.monotonic(),
                    duration_s=duration,
                    controlled_start=origin.copy(),
                    controlled_goal=requested.copy(),
                    position_residual_m=float(result.position_residual_m),
                    orientation_residual_rad=float(result.orientation_residual_rad),
                    ik_iterations=int(result.iterations),
                    sigma_min=float(result.sigma_min),
                    target_revision=self.target_revision,
                    last_replan_monotonic=time.monotonic(),
                )
                if self.execution_mode == TRANSIT_SPEED:
                    velocity_limits = synchronized_velocity_limits(
                        q_start,
                        q_goal,
                        duration,
                        self._provider_pos_vel_caps_locked(),
                        stationary_joint_limit_rad_s=float(self.config["trajectory"]["stationary_joint_velocity_limit_rad_s"]),
                    )
                    keepalive_hz = float(self.config["trajectory"]["latched_keepalive_hz"])
                    self.latched_endpoint = LatchedEndpointCommand.create(
                        "POSITION_VELOCITY_LIMITED",
                        q_start,
                        q_goal,
                        velocity_limits,
                        keepalive_period_s=1.0 / keepalive_hz,
                    )
                    self.hybrid_policy = None
                    self.hybrid_started_monotonic = time.monotonic()
                    self.control_state = "EXECUTING_POS_VEL_ENDPOINT"
                elif self.execution_mode == CONTACT_WORK:
                    velocity_limits = synchronized_velocity_limits(
                        q_start,
                        q_goal,
                        duration,
                        self._provider_rate_caps_locked(),
                        stationary_joint_limit_rad_s=float(
                            self.config["trajectory"]["stationary_joint_velocity_limit_rad_s"]
                        ),
                    )
                    torque_ratios = self._contact_force_position_ratios_locked(
                        state,
                        q_goal,
                    )
                    keepalive_hz = float(self.config["trajectory"]["latched_keepalive_hz"])
                    self.latched_endpoint = LatchedEndpointCommand.create(
                        "POSITION_EFFORT_LIMITED",
                        q_start,
                        q_goal,
                        velocity_limits,
                        keepalive_period_s=1.0 / keepalive_hz,
                        torque_limit_ratios=torque_ratios,
                    )
                    self.contact_torque_limit_ratios = torque_ratios.copy()
                    self.contact_residual_nm = np.zeros(6, dtype=float)
                    self.contact_limit_violations = []
                    self.hybrid_policy = None
                    self.control_state = "EXECUTING_POS_TOR_ENDPOINT"
                else:
                    self.latched_endpoint = None
                    self.hybrid_policy = None
                    self.control_state = "EXECUTING_MIT_ONESHOT" if self.interaction_mode == INTERACTION_ONE_SHOT else "EXECUTING_MIT_HOLD_LB"
                self.float_confirmed = False
                self.fault_reason = None
                self.last_error = None
                self.health = "HEALTHY"
            self._start_trajectory_thread()
        except Exception as exc:
            with self.lock:
                self.rejected_count += 1
                self.last_error = str(exc)
                self.health = "DEGRADED"
            self._request_float_once(f"commit rejected: {exc}")

    def _replan_continuous(self) -> None:
        started = time.monotonic()
        try:
            state = self.basic.state()
            now = time.monotonic()
            with self.lock:
                plan = self.trajectory
                if plan is None or plan.interaction_mode != INTERACTION_HOLD_LB or not self.input.lb_pressed:
                    return
                if plan.execution_mode == CONTACT_WORK:
                    return
                self.basic_state = state
                self.last_state_success = now
                self._assert_motion_prerequisites_locked(now)
                if self.kinematics is None or self.staged_target is None:
                    raise RuntimeError("kinematics or target unavailable during replan")
                measured_q = self._measured_positions_locked()[:6].copy()
                origin = self.kinematics.controlled_frame(measured_q, self._tool_to_control_locked())
                requested, clamped = self._clamped_target_locked(origin, self.staged_target)
                if clamped:
                    self.staged_target = requested.copy()
                if plan.execution_mode == CONTACT_WORK:
                    if self.torque_baseline is None:
                        raise PlanningRejected("CONTACT_WORK torque baseline was lost")
                    baseline_frame = self.kinematics.controlled_frame(
                        self.torque_baseline.positions_rad,
                        self._tool_to_control_locked(),
                    )
                    maximum_contact = float(self.config["contact"]["maximum_translation_m"])
                    from_baseline = float(
                        np.linalg.norm(requested[:3, 3] - baseline_frame[:3, 3])
                    )
                    if from_baseline > maximum_contact:
                        raise PlanningRejected(
                            f"continuous CONTACT_WORK target is {from_baseline:.4f} m "
                            f"from its baseline, above {maximum_contact:.4f} m"
                        )
                result = self._solve_target_locked(measured_q, requested)
                if plan.execution_mode in {TRANSIT_SPEED, CONTACT_WORK}:
                    # Latched endpoint speed must be synchronized from the
                    # physical position, not from an earlier endpoint that the
                    # motor may not have reached yet.
                    q_start = measured_q.copy()
                    qd_start = np.zeros(6, dtype=float)
                else:
                    q_start = self.commanded_q.copy() if self.commanded_q is not None else measured_q
                    qd_start = self.commanded_qd.copy() if self.commanded_qd is not None else np.zeros(6, dtype=float)
                duration = self._continuous_horizon_locked(q_start, result.q_goal)
                new_endpoint = None
                new_contact_ratios = None
                if plan.execution_mode == TRANSIT_SPEED:
                    velocity_limits = synchronized_velocity_limits(
                        measured_q,
                        result.q_goal,
                        duration,
                        self._provider_pos_vel_caps_locked(),
                        stationary_joint_limit_rad_s=float(
                            self.config["trajectory"]["stationary_joint_velocity_limit_rad_s"]
                        ),
                    )
                    new_endpoint = LatchedEndpointCommand.create(
                        "POSITION_VELOCITY_LIMITED",
                        measured_q,
                        result.q_goal,
                        velocity_limits,
                        keepalive_period_s=1.0 / float(
                            self.config["trajectory"]["latched_keepalive_hz"]
                        ),
                    )
                elif plan.execution_mode == CONTACT_WORK:
                    velocity_limits = synchronized_velocity_limits(
                        measured_q,
                        result.q_goal,
                        duration,
                        self._provider_rate_caps_locked(),
                        stationary_joint_limit_rad_s=float(
                            self.config["trajectory"]["stationary_joint_velocity_limit_rad_s"]
                        ),
                    )
                    new_contact_ratios = self._contact_force_position_ratios_locked(
                        state,
                        result.q_goal,
                    )
                    new_endpoint = LatchedEndpointCommand.create(
                        "POSITION_EFFORT_LIMITED",
                        measured_q,
                        result.q_goal,
                        velocity_limits,
                        keepalive_period_s=1.0 / float(
                            self.config["trajectory"]["latched_keepalive_hz"]
                        ),
                        torque_limit_ratios=new_contact_ratios,
                    )
                plan.q_start = q_start
                plan.qd_start = qd_start
                plan.q_goal = result.q_goal.copy()
                plan.segment_started_monotonic = now
                plan.duration_s = duration
                plan.controlled_start = origin.copy()
                plan.controlled_goal = requested.copy()
                plan.position_residual_m = float(result.position_residual_m)
                plan.orientation_residual_rad = float(result.orientation_residual_rad)
                plan.ik_iterations = int(result.iterations)
                plan.sigma_min = float(result.sigma_min)
                plan.replan_count += 1
                plan.last_replan_monotonic = now
                plan.target_revision = self.target_revision
                self.goal_q = result.q_goal.copy()
                self.last_committed_target = requested.copy()
                self.last_commit_origin = origin.copy()
                self.last_target_clamped = clamped
                self.last_position_residual_m = float(result.position_residual_m)
                self.last_orientation_residual_rad = float(result.orientation_residual_rad)
                self.last_ik_iterations = int(result.iterations)
                self.last_sigma_min = float(result.sigma_min)
                self.live_replan_count += 1
                if new_endpoint is not None:
                    self.latched_endpoint = new_endpoint
                    self.commanded_q = result.q_goal.copy()
                    self.commanded_qd = np.zeros(6, dtype=float)
                    if new_contact_ratios is not None:
                        self.contact_torque_limit_ratios = new_contact_ratios.copy()
                        self.control_state = "EXECUTING_POS_TOR_ENDPOINT"
                    else:
                        self.control_state = "EXECUTING_POS_VEL_ENDPOINT"
            elapsed_ms = (time.monotonic() - started) * 1000.0
            with self.lock:
                self.last_replan_duration_ms = elapsed_ms
                self.max_replan_duration_ms = max(self.max_replan_duration_ms, elapsed_ms)
        except PlanningRejected as exc:
            with self.lock:
                self.rejected_count += 1
                self.last_error = f"continuous replan rejected; keeping last valid plan: {exc}"
                self.health = "DEGRADED"
        except Exception as exc:
            with self.lock:
                plan = self.trajectory
                if plan is not None and plan.execution_mode in {TRANSIT_SPEED, CONTACT_WORK}:
                    self.rejected_count += 1
                    mode_label = (
                        "POS_TOR" if plan.execution_mode == CONTACT_WORK else "POS_VEL"
                    )
                    self.last_error = (
                        f"continuous {mode_label} replan rejected; keeping the last "
                        f"valid endpoint active: {exc}"
                    )
                    self.health = "DEGRADED"
                    self.control_state = (
                        "HOLDING_LAST_VALID_POS_TOR_ENDPOINT"
                        if plan.execution_mode == CONTACT_WORK
                        else "HOLDING_LAST_VALID_POS_VEL_ENDPOINT"
                    )
                    return
            self._fault_to_float(f"continuous replan fault: {exc}")

    # ------------------------------------------------------------------
    # MIT trajectory streaming
    # ------------------------------------------------------------------
    def _start_trajectory_thread(self) -> None:
        with self.lock:
            if self.trajectory_thread and self.trajectory_thread.is_alive():
                raise RuntimeError("trajectory worker is already active")
            latched = bool(
                self.trajectory
                and self.trajectory.execution_mode in {TRANSIT_SPEED, CONTACT_WORK}
            )
            target = self._pos_vel_trajectory_loop if latched else self._trajectory_loop
            name = "arm-latched-endpoint-hold" if latched else "arm-mit-trajectory"
            self.trajectory_thread = threading.Thread(target=target, name=name, daemon=True)
            self.trajectory_thread.start()

    def _pos_vel_trajectory_loop(self) -> None:
        """Keep a POS_VEL or POS_TOR endpoint active until operator release."""
        poll_period = 1.0 / max(float(self.config["basic_state_rate_hz"]), 1.0)
        next_tick = time.monotonic()
        observed_endpoint: LatchedEndpointCommand | None = None
        stable_samples = 0
        arrival_reported = False
        timeout_reported = False
        endpoint_started = time.monotonic()
        position_tolerance = self.config["trajectory"]["arrival_position_tolerance_rad"]
        velocity_tolerance = self.config["trajectory"]["arrival_velocity_tolerance_rad_s"]
        required_stable_samples = int(
            self.config["trajectory"].get("arrival_stable_samples", 10)
        )
        last_accepted_endpoint: LatchedEndpointCommand | None = None
        try:
            while not self.stop_event.is_set():
                now = time.monotonic()
                if now < next_tick:
                    self.stop_event.wait(next_tick - now)
                    continue
                next_tick = now + poll_period
                state = self.basic.state()
                auto_float_after_arrival = False
                auto_float_after_contact = False
                contact_completion_reason = ""
                commands = None
                with self.lock:
                    plan = self.trajectory
                    endpoint = self.latched_endpoint
                    if (
                        plan is None
                        or plan.execution_mode not in {TRANSIT_SPEED, CONTACT_WORK}
                        or endpoint is None
                    ):
                        return
                    self.basic_state = state
                    self.last_state_success = now
                    self._assert_motion_prerequisites_locked(now)
                    measured_q = self._measured_positions_locked()[:6].copy()
                    measured_qd = self._measured_velocities_locked()[:6].copy()
                    if plan.execution_mode == CONTACT_WORK:
                        self._update_contact_monitor_locked(state)
                    if endpoint is not observed_endpoint:
                        observed_endpoint = endpoint
                        stable_samples = 0
                        arrival_reported = False
                        timeout_reported = False
                        endpoint_started = now
                    timeout = max(
                        float(self.config["hybrid_approach"]["approach_timeout_minimum_s"]),
                        plan.duration_s * float(self.config["hybrid_approach"]["approach_timeout_multiplier"]),
                    )
                    arrived = endpoint.arrived(
                        measured_q,
                        measured_qd,
                        position_tolerance_rad=position_tolerance,
                        velocity_tolerance_rad_s=velocity_tolerance,
                    )
                    stable_samples = stable_samples + 1 if arrived else 0
                    contact_pulse_complete = (
                        plan.execution_mode == CONTACT_WORK
                        and plan.interaction_mode == INTERACTION_ONE_SHOT
                        and now - endpoint_started >= plan.duration_s
                    )
                    if contact_pulse_complete:
                        self.control_state = "FLOATING_AFTER_POS_TOR_ONESHOT"
                        auto_float_after_contact = True
                        contact_completion_reason = "one-shot CONTACT_WORK duration complete"
                    elif stable_samples >= required_stable_samples:
                        arrival_reported = True
                        if (
                            plan.execution_mode == TRANSIT_SPEED
                            and plan.interaction_mode == INTERACTION_ONE_SHOT
                        ):
                            self.control_state = "FLOATING_AFTER_POS_VEL_ARRIVAL"
                            auto_float_after_arrival = True
                        elif (
                            plan.execution_mode == CONTACT_WORK
                            and self.contact_saturated_joint_indices
                        ):
                            self.control_state = "HOLDING_POS_TOR_ENDPOINT_SATURATED"
                        else:
                            self.control_state = (
                                "HOLDING_POS_TOR_ENDPOINT"
                                if plan.execution_mode == CONTACT_WORK
                                else "HOLDING_POS_VEL_ENDPOINT"
                            )
                        self.commanded_q = endpoint.q_goal.copy()
                        self.commanded_qd = np.zeros(6, dtype=float)
                        plan.final_frame_sent = True
                        if self.fault_reason is None:
                            self.health = "HEALTHY"
                            self.last_error = None
                    elif not timeout_reported and now - endpoint_started > timeout:
                        timeout_reported = True
                        mode_label = (
                            "POS_TOR"
                            if plan.execution_mode == CONTACT_WORK
                            else "POS_VEL"
                        )
                        self.control_state = f"HOLDING_{mode_label}_ENDPOINT_UNCONFIRMED"
                        self.last_error = (
                            f"{mode_label} endpoint was not confirmed within {timeout:.2f} s; "
                            "the endpoint remains actively held until explicit release"
                        )
                        self.health = "DEGRADED"
                    elif timeout_reported:
                        self.control_state = (
                            "HOLDING_POS_TOR_ENDPOINT_UNCONFIRMED"
                            if plan.execution_mode == CONTACT_WORK
                            else "HOLDING_POS_VEL_ENDPOINT_UNCONFIRMED"
                        )
                    elif not arrival_reported:
                        self.control_state = (
                            "EXECUTING_POS_TOR_ENDPOINT"
                            if plan.execution_mode == CONTACT_WORK
                            else "EXECUTING_POS_VEL_ENDPOINT"
                        )
                    should_send = (
                        not auto_float_after_arrival
                        and not auto_float_after_contact
                        and endpoint.should_send(now)
                    )
                    commands = (
                        self._append_latched_gripper_locked(endpoint.commands())
                        if should_send
                        else None
                    )
                    if commands is not None:
                        plan.frames_attempted += 1

                if auto_float_after_arrival or auto_float_after_contact:
                    completion_reason = (
                        contact_completion_reason
                        if auto_float_after_contact
                        else "one-shot POS_VEL endpoint arrived"
                    )
                    float_ok = self._request_float_once(completion_reason)
                    with self.lock:
                        if self.trajectory is plan:
                            self.last_completed_trajectory = self._plan_summary_locked(
                                plan,
                                float_ok,
                            )
                            self.trajectory = None
                            self.latched_endpoint = None
                            self.hybrid_policy = None
                            self.commanded_qd = np.zeros(6, dtype=float)
                            self.control_state = (
                                "TARGET_EDIT"
                                if self.residency == "HOT"
                                else "IDLE_FLOAT"
                            )
                            if float_ok and self.fault_reason is None:
                                self.health = "HEALTHY"
                    return

                if commands is not None:
                    started = time.monotonic()
                    try:
                        with self.command_gate_lock:
                            with self.lock:
                                if self.trajectory is not plan or not self.engaged or not self._platform_ready_locked():
                                    return
                            self.basic.command(commands, int(self.config["command_timeout_ms"]))
                    except HttpStatusError as exc:
                        if exc.status_code != 400:
                            raise
                        with self.lock:
                            if self.trajectory is not plan:
                                return
                            self.rejected_count += 1
                            mode_label = (
                                "POS_TOR"
                                if plan.execution_mode == CONTACT_WORK
                                else "POS_VEL"
                            )
                            self.last_error = (
                                f"Basic rejected a {mode_label} endpoint; "
                                f"keeping the last accepted endpoint active: {exc}"
                            )
                            self.health = "DEGRADED"
                            if last_accepted_endpoint is None:
                                self.trajectory = None
                                self.latched_endpoint = None
                                self.control_state = "TARGET_EDIT"
                                return
                            last_accepted_endpoint.last_sent_monotonic = 0.0
                            self.latched_endpoint = last_accepted_endpoint
                            plan.q_goal = last_accepted_endpoint.q_goal.copy()
                            self.goal_q = last_accepted_endpoint.q_goal.copy()
                            self.commanded_q = last_accepted_endpoint.q_goal.copy()
                            self.commanded_qd = np.zeros(6, dtype=float)
                            self.control_state = (
                                "HOLDING_LAST_VALID_POS_TOR_ENDPOINT"
                                if plan.execution_mode == CONTACT_WORK
                                else "HOLDING_LAST_VALID_POS_VEL_ENDPOINT"
                            )
                        continue
                    sent_at = time.monotonic()
                    with self.lock:
                        if self.trajectory is not plan:
                            return
                        endpoint.mark_sent(sent_at)
                        plan.frames_sent += 1
                        self.command_count += 1
                        self.last_command_sent_monotonic = sent_at
                        latency_ms = (sent_at - started) * 1000.0
                        self.last_command_latency_ms = latency_ms
                        self.max_command_latency_ms = max(self.max_command_latency_ms, latency_ms)
                        last_accepted_endpoint = endpoint
        except LeaseLostError as exc:
            self._handle_lease_loss(f"{exc.error_code}: {exc.reason}")
        except Exception as exc:
            self._fault_to_float(f"latched endpoint fault: {exc}")

    def _confirm_basic_arm_mode(
        self,
        expected_mode: str,
        hold_q: np.ndarray,
        *,
        timeout_s: float,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Hold one arm target until Basic confirms all six motor modes."""
        deadline=time.monotonic()+timeout_s
        period=1.0/max(float(self.config["trajectory"]["send_rate_hz"]),1.0)
        next_send=0.0
        last_modes: list[Any] | None=None
        stable_mode_samples=0
        required_stable_samples=int(
            self.config["hybrid_approach"].get("post_switch_stable_samples",10)
        )
        velocity_limits=np.asarray(
            self.config["hybrid_approach"]["completion_velocity_rad_s"],
            dtype=float,
        )
        while time.monotonic()<deadline and not self.stop_event.is_set():
            state=self.basic.state()
            now=time.monotonic()
            with self.lock:
                if self.trajectory is None or not self.engaged:
                    raise RuntimeError("mode confirmation was cancelled")
                self.basic_state=state
                self.last_state_success=now
                self._assert_motion_prerequisites_locked(now)
                modes=state.get("active_command_modes")
                # Compatibility with an older Basic test double. The deployed
                # Basic 0.1.20 state always includes explicit active modes.
                if modes is None:
                    return (
                        self._measured_positions_locked()[:6].copy(),
                        self._measured_velocities_locked()[:6].copy(),
                    )
                last_modes=list(modes)
                transition=state.get("mode_transition", {})
                if (
                    len(last_modes)>=6
                    and all(str(mode)==expected_mode for mode in last_modes[:6])
                    and not bool(transition.get("active",False))
                ):
                    measured_velocity=self._measured_velocities_locked()[:6].copy()
                    stable_mode_samples=(
                        stable_mode_samples+1
                        if np.all(np.abs(measured_velocity)<=velocity_limits)
                        else 0
                    )
                    if stable_mode_samples>=required_stable_samples:
                        return (
                            self._measured_positions_locked()[:6].copy(),
                            np.zeros(6,dtype=float),
                        )
                else:
                    stable_mode_samples=0
                commands=None
                if now>=next_send:
                    commands=self._build_commands_locked(hold_q,np.zeros(6,dtype=float))
                    next_send=now+period
            if commands is not None:
                with self.command_gate_lock:
                    self.basic.command(commands,int(self.config["command_timeout_ms"]))
                with self.lock:
                    self.command_count+=1
                    self.last_command_sent_monotonic=time.monotonic()
            self.stop_event.wait(min(period,0.02))
        raise RuntimeError(
            f"Basic did not confirm arm mode {expected_mode} within {timeout_s:.2f} s "
            f"(last_modes={last_modes}, stable_mode_samples={stable_mode_samples}/"
            f"{required_stable_samples})"
        )

    @staticmethod
    def _hermite(q0: np.ndarray, v0: np.ndarray, q1: np.ndarray, duration: float, elapsed: float) -> tuple[np.ndarray, np.ndarray, float]:
        t = max(duration, 1e-6)
        u = float(np.clip(elapsed / t, 0.0, 1.0))
        h00 = 2*u**3 - 3*u**2 + 1
        h10 = u**3 - 2*u**2 + u
        h01 = -2*u**3 + 3*u**2
        q = h00*q0 + h10*t*v0 + h01*q1
        dh00 = 6*u**2 - 6*u
        dh10 = 3*u**2 - 4*u + 1
        dh01 = -6*u**2 + 6*u
        qd = (dh00*q0 + dh10*t*v0 + dh01*q1) / t
        return q, qd, u

    def _trajectory_loop(self) -> None:
        period = 1.0 / max(float(self.config["trajectory"]["send_rate_hz"]), 1.0)
        max_gap = float(self.config["trajectory"]["maximum_command_gap_ms"]) / 1000.0
        next_tick = time.monotonic()
        first_frame = True
        try:
            while not self.stop_event.is_set():
                with self.lock:
                    plan = self.trajectory
                    if plan is None:
                        return
                now = time.monotonic()
                if now < next_tick:
                    self.stop_event.wait(next_tick - now)
                    continue
                lateness = max(0.0, now - next_tick)
                self.max_send_lateness_ms = max(self.max_send_lateness_ms, lateness * 1000.0)
                if lateness >= period:
                    skipped = int(lateness // period)
                    with self.lock:
                        if self.trajectory is plan:
                            plan.frames_skipped += skipped
                    next_tick += skipped * period
                next_tick += period
                if not first_frame and self.last_command_sent_monotonic:
                    gap = now - self.last_command_sent_monotonic
                    if gap > max_gap:
                        raise RuntimeError(f"trajectory command gap {gap*1000.0:.1f} ms exceeds {max_gap*1000.0:.1f} ms")

                with self.lock:
                    self._assert_motion_prerequisites_locked(now)
                    if self.trajectory is not plan:
                        return
                    if plan.interaction_mode == INTERACTION_HOLD_LB and not self.input.lb_pressed:
                        return
                    elapsed = max(0.0, now - plan.segment_started_monotonic)
                    q_raw, qd_raw, progress = self._hermite(plan.q_start, plan.qd_start, plan.q_goal, plan.duration_s, elapsed)
                    previous_q = self.commanded_q.copy() if self.commanded_q is not None else plan.q_start.copy()
                    caps = self._provider_rate_caps_locked()
                    maximum_step = caps * period
                    q_command = previous_q + np.clip(q_raw - previous_q, -maximum_step, maximum_step)
                    qd_command = np.clip((q_command - previous_q) / period, -caps, caps)
                    self.commanded_q = q_command.copy()
                    self.commanded_qd = qd_command.copy()
                    measured = self._measured_positions_locked()[:6]
                    tracking = float(np.max(np.abs(q_command - measured)))
                    self.last_tracking_error_rad = tracking
                    self.max_tracking_error_rad = max(self.max_tracking_error_rad, tracking)
                    plan.frames_attempted += 1
                    commands = self._build_commands_locked(q_command, qd_command)

                started = time.monotonic()
                with self.command_gate_lock:
                    with self.lock:
                        if self.trajectory is not plan:
                            return
                        if not self.engaged or not self._platform_ready_locked() or self.basic.lease_snapshot() is None:
                            return
                    self.basic.command(commands, int(self.config["command_timeout_ms"]))
                latency_ms = (time.monotonic() - started) * 1000.0
                sent_at = time.monotonic()
                with self.lock:
                    if self.trajectory is not plan:
                        return
                    plan.frames_sent += 1
                    self.command_count += 1
                    self.last_command_latency_ms = latency_ms
                    self.max_command_latency_ms = max(self.max_command_latency_ms, latency_ms)
                    self.last_command_sent_monotonic = sent_at
                    one_shot_done = plan.interaction_mode == INTERACTION_ONE_SHOT and progress >= 1.0
                    if one_shot_done:
                        plan.final_frame_sent = True
                        break
                first_frame = False

            with self.lock:
                plan = self.trajectory
                if plan is None:
                    return
                if plan.interaction_mode == INTERACTION_HOLD_LB:
                    return
                self.control_state = "FLOATING_AFTER_TRAJECTORY"
            float_ok = self._request_float_once("one-shot MIT trajectory complete")
            with self.lock:
                plan = self.trajectory
                if plan is not None:
                    self.last_completed_trajectory = self._plan_summary_locked(plan, float_ok)
                self.trajectory = None
                self.latched_endpoint = None
                self.hybrid_policy = None
                self.commanded_qd = np.zeros(6, dtype=float)
                self.control_state = "TARGET_EDIT" if self.residency == "HOT" else "IDLE_FLOAT"
                if float_ok and self.fault_reason is None:
                    self.health = "HEALTHY"
        except LeaseLostError as exc:
            self._handle_lease_loss(f"{exc.error_code}: {exc.reason}")
        except Exception as exc:
            self._fault_to_float(f"trajectory fault: {exc}")

    def _plan_summary_locked(self, plan: TrajectoryPlan, float_ok: bool) -> dict[str, Any]:
        return {
            "execution_mode": plan.execution_mode,
            "interaction_mode": plan.interaction_mode,
            "ik_mode": plan.ik_mode,
            "duration_s": plan.duration_s,
            "q_start_rad": plan.q_start.tolist(),
            "q_goal_rad": plan.q_goal.tolist(),
            "controlled_start_position_m": plan.controlled_start[:3, 3].tolist(),
            "controlled_goal_position_m": plan.controlled_goal[:3, 3].tolist(),
            "position_residual_m": plan.position_residual_m,
            "orientation_residual_rad": plan.orientation_residual_rad,
            "replan_count": plan.replan_count,
            "frames_attempted": plan.frames_attempted,
            "frames_sent": plan.frames_sent,
            "frames_skipped": plan.frames_skipped,
            "completed_at_us": time.time_ns() // 1000,
            "float_confirmed": bool(float_ok),
        }

    # ------------------------------------------------------------------
    # MIT command generation
    # ------------------------------------------------------------------
    def _gain_profile_locked(self) -> list[dict[str, Any]]:
        profile: list[dict[str, Any]] = []
        for index in range(6):
            joint = self.basic_model["joints"][index]
            defaults = dict(joint.get("default_test", {}))
            caps = dict(joint.get("provider_test_caps", {}))
            motor_limits = dict(joint.get("motor_limits", {}))
            base_kp = float(defaults.get("kp", 0.0))
            base_kd = float(defaults.get("kd", 0.0))
            requested_kp = base_kp * self.kp_multiplier
            protocol = motor_limits.get("mit_kp_protocol_range", [0.0, float("inf")])
            protocol_cap = float(protocol[1]) if len(protocol) >= 2 else float("inf")
            provider_cap = float(caps.get("max_kp", protocol_cap))
            kp_cap = min(provider_cap, protocol_cap)
            kp_floor = float(caps.get("min_kp", base_kp))
            effective_kp = float(np.clip(requested_kp, kp_floor, kp_cap))
            requested_kd = base_kd * math.sqrt(max(self.kp_multiplier, 0.0))
            kd_cap = float(caps.get("max_kd", base_kd))
            effective_kd = float(np.clip(requested_kd, 0.0, kd_cap))
            profile.append({
                "joint_index": index,
                "joint_name": joint.get("name", f"joint{index+1}"),
                "base_kp": base_kp,
                "requested_kp": requested_kp,
                "effective_kp": effective_kp,
                "kp_cap": kp_cap,
                "kp_clamped": abs(effective_kp - requested_kp) > 1e-9,
                "base_kd": base_kd,
                "requested_kd": requested_kd,
                "effective_kd": effective_kd,
                "kd_cap": kd_cap,
                "kd_clamped": abs(effective_kd - requested_kd) > 1e-9,
                "tracking_effort_limit_nm": float(caps.get("mit_tracking_effort_limit_nm", 0.0)),
            })
        return profile

    def _build_commands_locked(self, q_target: np.ndarray, qd_target: np.ndarray) -> list[dict[str, Any]]:
        gains = self._gain_profile_locked()
        rates = self._provider_rate_caps_locked()
        commands = []
        for index, gain in enumerate(gains):
            commands.append({
                "joint_index": index,
                "mode": MODE_MIT,
                "values": {
                    "position_rad": float(q_target[index]),
                    "velocity_rad_s": float(qd_target[index]),
                    "target_rate_limit_rad_s": float(rates[index]),
                    "kp": float(gain["effective_kp"]),
                    "kd": float(gain["effective_kd"]),
                    # Basic owns calibrated arm + declared payload gravity feed-forward.
                    "feedforward_torque_nm": 0.0,
                },
            })
        return self._append_latched_gripper_locked(commands)

    def _provider_rate_caps_locked(self) -> np.ndarray:
        result = []
        for index in range(6):
            joint = self.basic_model["joints"][index]
            calibrated = dict(joint.get("calibrated", {}))
            caps = dict(joint.get("provider_test_caps", {}))
            candidates = [
                float(calibrated.get("provider_velocity_cap_rad_s", float("inf"))),
                float(caps.get("max_velocity_rad_s", float("inf"))),
            ]
            value = min(candidates)
            if not np.isfinite(value) or value <= 0.0:
                raise RuntimeError(f"joint {index + 1} has no valid provider rate cap")
            # Stay strictly below Basic's inclusive decimal cap so a JSON
            # round-trip cannot turn an exact boundary into a rejection.
            result.append(float(np.nextafter(value, 0.0)))
        return np.asarray(result, dtype=float)

    def _provider_pos_vel_caps_locked(self) -> np.ndarray:
        control = dict(self.basic_model.get("control", {}))
        configured = np.asarray(
            control.get("physical_test_pos_vel_cap_rad_s", []),
            dtype=float,
        )
        if configured.shape != (7,) or not np.all(np.isfinite(configured)):
            raise RuntimeError("Basic model does not expose seven physical-test POS_VEL caps")
        result = []
        for index in range(6):
            joint = self.basic_model["joints"][index]
            motor_limits = dict(joint.get("motor_limits", {}))
            motor_vmax = float(
                motor_limits.get("configured_vmax_rad_s", float("inf"))
            )
            value = min(float(configured[index]), motor_vmax)
            if not np.isfinite(value) or value <= 0.0:
                raise RuntimeError(
                    f"joint {index + 1} has no valid physical-test POS_VEL cap"
                )
            result.append(float(np.nextafter(value, 0.0)))
        return np.asarray(result, dtype=float)

    def _contact_budget_configured_locked(self) -> bool:
        contact = self.config["contact"]
        mode = str(contact.get("budget_mode", "JOINT_6")).upper()
        if mode == "JOINT_6":
            return contact.get("task_torque_budget_nm") is not None
        if mode == "WRENCH_6":
            force = contact.get("wrench_force_budget_n")
            torque = contact.get("wrench_torque_budget_nm")
            return (
                force is not None
                and torque is not None
                and any(float(value) > 0.0 for value in [*force, *torque])
            )
        if mode == "ISOTROPIC_2":
            force = contact.get("isotropic_force_budget_n")
            torque = contact.get("isotropic_torque_budget_nm")
            return (
                force is not None
                and torque is not None
                and (float(force) > 0.0 or float(torque) > 0.0)
            )
        return False

    def _contact_joint_budget_locked(self, q_reference: np.ndarray) -> np.ndarray:
        contact = self.config["contact"]
        mode = str(contact.get("budget_mode", "JOINT_6")).upper()
        if mode == "JOINT_6":
            raw = contact.get("task_torque_budget_nm")
            if raw is None:
                raise RuntimeError("CONTACT_WORK JOINT_6 torque budgets are unavailable")
            budget = np.asarray(raw, dtype=float)
        else:
            if self.kinematics is None:
                raise RuntimeError("CONTACT_WORK kinematics are unavailable")
            jacobian, controlled = self.kinematics.geometric_jacobian(
                q_reference,
                self._tool_to_control_locked(),
            )
            if mode == "WRENCH_6":
                force = contact.get("wrench_force_budget_n")
                torque = contact.get("wrench_torque_budget_nm")
                if force is None or torque is None:
                    raise RuntimeError("CONTACT_WORK WRENCH_6 budgets are incomplete")
                budget = cartesian_wrench_to_joint_budget(
                    jacobian,
                    controlled.transform[:3, :3],
                    force,
                    torque,
                    minimum_joint_budget_nm=contact["minimum_mapped_joint_budget_nm"],
                )
            elif mode == "ISOTROPIC_2":
                isotropic_force = contact.get("isotropic_force_budget_n")
                isotropic_torque = contact.get("isotropic_torque_budget_nm")
                if isotropic_force is None or isotropic_torque is None:
                    raise RuntimeError("CONTACT_WORK ISOTROPIC_2 budgets are incomplete")
                budget = isotropic_wrench_to_joint_budget(
                    jacobian,
                    controlled.transform[:3, :3],
                    float(isotropic_force),
                    float(isotropic_torque),
                    minimum_joint_budget_nm=contact["minimum_mapped_joint_budget_nm"],
                )
            else:
                raise RuntimeError(f"unsupported CONTACT_WORK budget mode {mode}")
        if budget.shape != (6,) or not np.all(np.isfinite(budget)) or np.any(budget <= 0.0):
            raise RuntimeError("CONTACT_WORK resolved joint budgets are invalid")
        return budget

    def _contact_force_position_ratios_locked(
        self,
        state: dict[str, Any],
        q_reference: np.ndarray,
    ) -> np.ndarray:
        if self.torque_baseline is None:
            raise RuntimeError("CONTACT_WORK torque baseline is unavailable")
        budget = self._contact_joint_budget_locked(q_reference)
        gravity = np.asarray(
            state.get("gravity_compensation", {}).get("total_nm", [])[:6],
            dtype=float,
        )
        if gravity.shape != (6,) or not np.all(np.isfinite(gravity)):
            raise RuntimeError("Basic gravity telemetry is unavailable for CONTACT_WORK")
        expected = self.torque_baseline.expected_torque(gravity)
        tmax = np.asarray(
            [
                float(self.basic_model["joints"][index]["motor_limits"]["configured_tmax_nm"])
                for index in range(6)
            ],
            dtype=float,
        )
        ratio_caps = np.asarray(
            self.basic_model.get("control", {}).get(
                "physical_test_pos_tor_ratio_cap",
                [],
            )[:6],
            dtype=float,
        )
        if ratio_caps.shape != (6,) or not np.all(np.isfinite(ratio_caps)):
            raise RuntimeError("Basic model does not expose six physical-test POS_TOR caps")
        measured_margin = 3.0 * self.torque_baseline.torque_mad_nm
        required = (np.abs(expected) + budget + measured_margin) / tmax
        saturated = [int(index) for index in np.flatnonzero(required > ratio_caps + 1e-12)]
        ratios = force_position_ratios(
            expected,
            budget,
            tmax,
            ratio_caps,
            margin_nm=measured_margin,
            saturate_at_caps=True,
        )
        self.contact_effective_joint_budget_nm = budget.copy()
        self.contact_saturated_joint_indices = saturated
        self.contact_saturation_reason = (
            "BASELINE_PLUS_BUDGET_CLAMPED_TO_PHYSICAL_CEILING"
            if saturated
            else None
        )
        return ratios

    def _update_contact_monitor_locked(self, state: dict[str, Any]) -> None:
        if self.torque_baseline is None:
            raise RuntimeError("CONTACT_WORK torque baseline was lost")
        budget = self.contact_effective_joint_budget_nm
        if budget is None:
            positions = np.asarray(state.get("positions_rad", [])[:6], dtype=float)
            if positions.shape != (6,) or not np.all(np.isfinite(positions)):
                raise RuntimeError("CONTACT_WORK effective joint torque budgets are unavailable")
            budget = self._contact_joint_budget_locked(positions)
            self.contact_effective_joint_budget_nm = budget.copy()
        torques = np.asarray(state.get("torques_nm", [])[:6], dtype=float)
        gravity = np.asarray(
            state.get("gravity_compensation", {}).get("total_nm", [])[:6],
            dtype=float,
        )
        if (
            torques.shape != (6,)
            or gravity.shape != (6,)
            or not np.all(np.isfinite(torques))
            or not np.all(np.isfinite(gravity))
        ):
            raise RuntimeError("CONTACT_WORK torque or gravity telemetry is unavailable")
        residual = self.torque_baseline.residual(torques, gravity)
        violations = torque_limit_violations(residual, budget)
        self.contact_residual_nm = residual
        self.contact_limit_violations = violations
        if violations:
            ratio_caps = np.asarray(
                self.basic_model.get("control", {}).get(
                    "physical_test_pos_tor_ratio_cap",
                    [],
                )[:6],
                dtype=float,
            )
            if ratio_caps.shape != (6,) or not np.all(np.isfinite(ratio_caps)):
                raise RuntimeError("Basic model does not expose six physical-test POS_TOR caps")
            endpoint = self.latched_endpoint
            if endpoint is not None and endpoint.torque_limit_ratios is not None:
                newly_saturated = [
                    index
                    for index in violations
                    if index not in self.contact_saturated_joint_indices
                ]
                endpoint.torque_limit_ratios[violations] = ratio_caps[violations]
                endpoint.last_sent_monotonic = 0.0
                self.contact_torque_limit_ratios = endpoint.torque_limit_ratios.copy()
                self.contact_saturated_joint_indices = sorted(
                    set(self.contact_saturated_joint_indices) | set(violations)
                )
                if newly_saturated:
                    self.contact_saturation_count += 1
                self.contact_saturation_reason = "RESIDUAL_BUDGET_EXCEEDED"
                self.control_state = "HOLDING_POS_TOR_ENDPOINT_SATURATED"

    # ------------------------------------------------------------------
    # Gripper hardware test
    # ------------------------------------------------------------------
    def _desired_gripper_action_locked(self) -> str | None:
        return self.gripper_ui_action or self.gripper_gamepad_action

    def _validate_gripper_against_model_locked(self) -> None:
        if not self.basic_model:
            return
        joint = self.basic_model["joints"][6]
        limits = joint.get("operational_limit_rad", joint.get("hard_limit_rad"))
        if not isinstance(limits, list) or len(limits) != 2:
            raise RuntimeError("Basic gripper limits are unavailable")
        low, high = (float(limits[0]), float(limits[1]))
        for label, value in (("open", self.gripper_open_position_rad), ("closed", self.gripper_closed_position_rad)):
            if value < low or value > high:
                raise ValueError(f"gripper {label} target {value:.4f} rad is outside Basic operational limits [{low:.4f}, {high:.4f}]")
        caps = dict(joint.get("provider_test_caps", {}))
        if self.gripper_velocity_limit_rad_s > float(caps.get("max_velocity_rad_s", float("inf"))):
            raise ValueError("gripper velocity exceeds the Basic provider test cap")
        if self.gripper_torque_limit_ratio > float(caps.get("max_torque_ratio", 1.0)):
            raise ValueError("gripper POS_TOR ratio exceeds the Basic provider test cap")
        if self.gripper_mit_kp < float(caps.get("min_kp", 0.0)) or self.gripper_mit_kp > float(caps.get("max_kp", float("inf"))):
            raise ValueError("gripper MIT Kp is outside the Basic provider test range")
        if self.gripper_mit_kd > float(caps.get("max_kd", float("inf"))):
            raise ValueError("gripper MIT Kd exceeds the Basic provider test cap")

    def _build_gripper_command_locked(self, action: str) -> list[dict[str, Any]]:
        self._validate_gripper_against_model_locked()
        target = self.gripper_open_position_rad if action == GRIPPER_OPEN else self.gripper_closed_position_rad
        if self.gripper_mode == GRIPPER_MIT:
            values = {
                "position_rad": target,
                "velocity_rad_s": 0.0,
                "target_rate_limit_rad_s": self.gripper_velocity_limit_rad_s,
                "kp": self.gripper_mit_kp,
                "kd": self.gripper_mit_kd,
                "feedforward_torque_nm": 0.0,
            }
            mode = MODE_MIT
        else:
            values = {
                "position_rad": target,
                "velocity_limit_rad_s": self.gripper_velocity_limit_rad_s,
                "torque_limit_ratio": self.gripper_torque_limit_ratio,
            }
            mode = "POSITION_EFFORT_LIMITED"
        self.gripper_target_rad = target
        return [{"joint_index": 6, "mode": mode, "values": values}]

    def _append_latched_gripper_locked(
        self,
        commands: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if self.gripper_active_action is None:
            return commands
        return [
            *commands,
            *self._build_gripper_command_locked(self.gripper_active_action),
        ]

    def _service_gripper(self, now: float) -> None:
        with self.lock:
            desired = self._desired_gripper_action_locked()
            active = self.gripper_active_action
            if desired is None and active is None:
                self.gripper_fault_latched = False
                return
            if self.gripper_fault_latched:
                return
            operation = desired or active
            if self.trajectory is not None:
                if active is not None:
                    self.control_state = f"GRIPPER_{self.gripper_mode}_{active}_LATCHED_WITH_ARM"
                    self.gripper_last_error = None
                else:
                    self.gripper_last_error = "gripper is blocked while an arm trajectory is active"
                return
            if not self.engaged:
                self.gripper_last_error = "Engage physical control before operating the gripper"
                return
            if now - self.gripper_last_send_monotonic < 1.0 / self.gripper_keepalive_hz:
                if desired is None and active is not None:
                    self.control_state = f"GRIPPER_{self.gripper_mode}_{active}_LATCHED"
                return
            if operation is None:
                self.gripper_last_error = "gripper is blocked while an arm trajectory is active"
                return
        try:
            with self.lock:
                self._assert_motion_prerequisites_locked(now)
                if self.trajectory is not None or not self.engaged:
                    return
                commands = self._build_gripper_command_locked(operation)
            with self.command_gate_lock:
                self.basic.command(commands, int(self.config["command_timeout_ms"]))
            with self.lock:
                self.gripper_active_action = operation
                self.gripper_last_send_monotonic = time.monotonic()
                self.gripper_command_count += 1
                self.gripper_last_error = None
                self.gripper_fault_latched = False
                suffix = "ACTIVE" if desired is not None else "LATCHED"
                self.control_state = f"GRIPPER_{self.gripper_mode}_{operation}_{suffix}"
        except Exception as exc:
            with self.lock:
                self.gripper_last_error = str(exc)
                self.gripper_fault_latched = True
                self.gripper_active_action = None
                self.gripper_target_rad = None
            self._request_float_once(f"gripper command failed: {exc}")

    # ------------------------------------------------------------------
    # Safety / lease / float
    # ------------------------------------------------------------------
    def _platform_ready_locked(self) -> bool:
        safety = self.config["safety"]
        manager_ok = self.manager_registered or not safety.get("require_manager_for_motion", True)
        fabric_ok = self.fabric_ready or not safety.get("require_fabric_for_motion", True)
        return manager_ok and fabric_ok and not self.motion_inhibited

    def _assert_motion_prerequisites_locked(self, now: float) -> None:
        if self.basic.lease_snapshot() is None:
            raise RuntimeError("Basic Controller lease is unavailable")
        if not self._platform_ready_locked():
            raise RuntimeError("Manager and Fabric must be healthy and motion inhibit must be clear")
        age_ms = (now - self.last_state_success) * 1000.0 if self.last_state_success else float("inf")
        if age_ms > float(self.config["max_basic_state_age_ms"]):
            raise RuntimeError(f"Basic Controller state is stale ({int(age_ms)} ms)")
        positions = self._measured_positions_locked()
        velocities = self._measured_velocities_locked()
        if not np.all(np.isfinite(positions)) or not np.all(np.isfinite(velocities)):
            raise RuntimeError("Basic Controller feedback contains non-finite values")
        basic_health = str(self.basic_state.get("health") or "")
        if basic_health in {"FAULTED", "UNHEALTHY"}:
            raise RuntimeError(f"Basic Controller health is {basic_health}: {self.basic_state.get('last_error')}")

    def _assert_workspace_target_locked(self, target: np.ndarray) -> None:
        workspace = self.config["workspace"]
        x, y, z = (float(value) for value in target)
        if abs(x) > float(workspace["abs_x_max_m"]):
            raise RuntimeError(f"target X {x:.4f} m is outside the configured workspace")
        if abs(y) > float(workspace["abs_y_max_m"]):
            raise RuntimeError(f"target Y {y:.4f} m is outside the configured workspace")
        if z < float(workspace["z_min_m"]) or z > float(workspace["z_max_m"]):
            raise RuntimeError(f"target Z {z:.4f} m is outside the configured workspace")

    def _cancel_active_trajectory(self, reason: str, *, request_float: bool) -> bool:
        with self.lock:
            plan = self.trajectory
            if plan is not None:
                self.last_completed_trajectory = self._plan_summary_locked(plan, False)
            self.trajectory = None
            self.latched_endpoint = None
            self.hybrid_policy = None
            self.commit_requested = False
            self.replan_requested = False
            self.commanded_qd = np.zeros(6, dtype=float) if self.commanded_qd is not None else None
            if self.residency == "HOT":
                self.control_state = "TARGET_EDIT"
        if request_float and self.basic.lease_snapshot() is not None:
            return self._request_float_once(reason)
        return True

    def _verify_float_once(self, reason: str) -> bool:
        return self._float_operation(reason, issue_request=False)

    def _request_float_once(self, reason: str) -> bool:
        return self._float_operation(reason, issue_request=True)

    def _float_operation(self, reason: str, *, issue_request: bool) -> bool:
        if self.basic.lease_snapshot() is None:
            with self.lock:
                self.float_confirmed = False
                self.last_float_reason = reason
            return False
        verified: dict[str, Any] | None = None
        with self.command_gate_lock:
            try:
                if issue_request:
                    self.basic.float(reason)
                deadline = time.monotonic() + float(self.config["safety"]["float_verify_timeout_ms"]) / 1000.0
                while time.monotonic() < deadline and not self.stop_event.is_set():
                    verified = self.basic.state()
                    state_name = str(verified.get("provider_state") or verified.get("state") or "")
                    pending_float_modes = verified.get("float_transition_pending_joint_indices", [])
                    if state_name == "SAFE_HOLD_GRAVITY_FLOAT" and not pending_float_modes:
                        break
                    time.sleep(0.03)
                else:
                    current = str((verified or {}).get("provider_state") or "unknown")
                    pending = (verified or {}).get("float_transition_pending_joint_indices", [])
                    raise RuntimeError(
                        f"Basic did not confirm completed SAFE_HOLD_GRAVITY_FLOAT "
                        f"(state={current}, pending_mode_joints={pending})"
                    )
            except Exception as exc:
                with self.lock:
                    if issue_request:
                        self.float_request_count += 1
                    self.float_failure_count += 1
                    self.float_confirmed = False
                    self.last_float_reason = reason
                    self.last_error = f"gravity-float failed: {exc}"
                    self.health = "DEGRADED"
                return False
        with self.lock:
            if issue_request:
                self.float_request_count += 1
            self.float_confirmed = True
            self.last_float_confirmed = time.monotonic()
            self.last_float_reason = reason
            if verified is not None:
                self.basic_state = verified
                self.last_state_success = time.monotonic()
            if self.fault_reason is None:
                self.health = "HEALTHY"
        return True

    def _fault_to_float(self, reason: str) -> None:
        with self.lock:
            self._set_fault_locked(reason)
            self.trajectory = None
            self.latched_endpoint = None
            self.hybrid_policy = None
            self.gripper_gamepad_action = None
            self.gripper_ui_action = None
            self.gripper_active_action = None
            self.gripper_target_rad = None
            self.gripper_fault_latched = False
        self._request_float_once(reason)

    def _set_fault_locked(self, reason: str) -> None:
        self.control_state = "FAULT_FLOAT"
        self.fault_reason = reason
        self.last_error = reason
        self.health = "DEGRADED"
        self.rejected_count += 1

    def _lease_loop(self) -> None:
        renew_period = float(self.config["lease_renew_period_ms"]) / 1000.0
        while not self.stop_event.wait(0.05):
            with self.lock:
                if self.residency != "HOT":
                    continue
                due = time.monotonic() - self.last_lease_attempt >= renew_period
            if not due:
                continue
            if self.basic.lease_snapshot() is None:
                self._ensure_runtime_lease(background=True)
            else:
                self._renew_lease_once()

    def _ensure_runtime_lease(self, background: bool = False) -> bool:
        if self.basic.lease_snapshot() is not None:
            return True
        now = time.monotonic()
        with self.lock:
            if background and now - self.last_lease_attempt < 1.0:
                return False
            self.last_lease_attempt = now
        try:
            lease = self.basic.acquire(
                f"{self.config['provider_id']}:{self.config['arm_id']}",
                int(self.config["lease_duration_ms"]),
            )
            self.basic.set_payload(self.payload_mass_kg, self.payload_com_tool_m.tolist())
        except Exception as exc:
            with self.lock:
                self.lease_acquire_failure_count += 1
                self.lease_state = "ACQUIRE_RETRY"
                self.last_error = f"lease acquisition failed: {exc}"
                self.health = "DEGRADED"
            if background:
                return False
            raise
        with self.lock:
            self.lease_state = "OWNED"
            self.last_lease_renew = time.monotonic()
            self.last_error = None
            if self.fault_reason is None:
                self.health = "HEALTHY"
        print(f"[mit-lease] acquired generation={lease.fencing_generation}")
        return True

    def _renew_lease_once(self) -> bool:
        started = time.monotonic()
        with self.lock:
            self.last_lease_attempt = started
        try:
            self.basic.renew(int(self.config["lease_duration_ms"]))
        except LeaseLostError as exc:
            with self.lock:
                self.lease_renew_failure_count += 1
            self._handle_lease_loss(f"{exc.error_code}: {exc.reason}")
            return False
        except Exception as exc:
            latency = (time.monotonic() - started) * 1000.0
            with self.lock:
                self.lease_renew_failure_count += 1
                self.lease_renew_latency_ms = latency
                self.max_lease_renew_latency_ms = max(self.max_lease_renew_latency_ms, latency)
                self.lease_state = "RENEW_RETRY"
                self.last_error = f"lease renewal transport error: {exc}"
                self.health = "DEGRADED"
            lease = self.basic.lease_snapshot()
            if lease is None or lease.expires_monotonic - time.monotonic() <= 1.5:
                self._handle_lease_loss(f"lease renewal became uncertain: {exc}")
            return False
        latency = (time.monotonic() - started) * 1000.0
        with self.lock:
            self.lease_renew_success_count += 1
            self.lease_renew_latency_ms = latency
            self.max_lease_renew_latency_ms = max(self.max_lease_renew_latency_ms, latency)
            self.last_lease_renew = time.monotonic()
            self.lease_state = "OWNED"
        return True

    def _handle_lease_loss(self, reason: str) -> None:
        self.basic.clear_lease()
        with self.lock:
            self.lease_state = "LOST"
            self.trajectory = None
            self.latched_endpoint = None
            self.hybrid_policy = None
            self.gripper_gamepad_action = None
            self.gripper_ui_action = None
            self.gripper_active_action = None
            self.gripper_target_rad = None
            self.gripper_fault_latched = False
            self.engaged = False
            self.control_state = "FAULT_FLOAT"
            self.fault_reason = f"Basic lease lost: {reason}"
            self.last_error = self.fault_reason
            self.health = "DEGRADED"
            self.rejected_count += 1
            self.float_confirmed = False

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _positions_from_state(state: dict[str, Any]) -> np.ndarray:
        positions = np.asarray(state.get("positions_rad", []), dtype=float)
        if positions.shape != (7,):
            raise RuntimeError("seven measured joint positions are required")
        return positions

    def _measured_positions_locked(self) -> np.ndarray:
        return self._positions_from_state(self.basic_state)

    def _measured_velocities_locked(self) -> np.ndarray:
        velocities = np.asarray(self.basic_state.get("velocities_rad_s", []), dtype=float)
        if velocities.shape != (7,):
            raise RuntimeError("seven measured joint velocities are required")
        return velocities

    @staticmethod
    def _frame_payload(frame: np.ndarray | None) -> dict[str, Any] | None:
        if frame is None:
            return None
        return {
            "position_m": frame[:3, 3].tolist(),
            "rotation_matrix": frame[:3, :3].tolist(),
            "rpy_rad": matrix_rpy(frame[:3, :3]).tolist(),
        }

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            state = copy.deepcopy(self.basic_state)
            positions = np.asarray(state.get("positions_rad", []), dtype=float)
            velocities = np.asarray(state.get("velocities_rad_s", []), dtype=float)
            torques = np.asarray(state.get("torques_nm", []), dtype=float)
            measured_q = positions[:6].tolist() if positions.size >= 6 else []
            velocity_q = velocities[:6].tolist() if velocities.size >= 6 else []
            torque_q = torques[:6].tolist() if torques.size >= 6 else []
            measured_points: list[list[float]] = []
            commanded_points: list[list[float]] = []
            measured_control: np.ndarray | None = None
            measured_tool: np.ndarray | None = None
            commanded_control: np.ndarray | None = None
            goal_control: np.ndarray | None = None
            if self.kinematics is not None and positions.size >= 6:
                frame = self.kinematics.evaluate(positions[:6])
                measured_points = [point.tolist() for point in frame.points]
                measured_tool = frame.transform.copy()
                measured_control = self.kinematics.controlled_frame(positions[:6], self._tool_to_control_locked())
            if self.kinematics is not None and self.commanded_q is not None:
                commanded_points = [point.tolist() for point in self.kinematics.evaluate(self.commanded_q).points]
                commanded_control = self.kinematics.controlled_frame(self.commanded_q, self._tool_to_control_locked())
            if self.kinematics is not None and self.goal_q is not None:
                goal_control = self.kinematics.controlled_frame(self.goal_q, self._tool_to_control_locked())
            plan = self.trajectory
            progress = None
            if plan is not None:
                progress = float(np.clip((time.monotonic() - plan.segment_started_monotonic) / max(plan.duration_s, 1e-9), 0.0, 1.0))
            lease = self.basic.lease_snapshot()
            input_age_ms = (time.monotonic() - self.input.received_monotonic) * 1000.0 if self.input.received_monotonic else None
            float_age_ms = (time.monotonic() - self.last_float_confirmed) * 1000.0 if self.last_float_confirmed else None
            try:
                rate_caps = (
                    (
                        self._provider_pos_vel_caps_locked()
                        if self.execution_mode == TRANSIT_SPEED
                        else self._provider_rate_caps_locked()
                    ).tolist()
                    if self.basic_model
                    else []
                )
                gains = self._gain_profile_locked() if self.basic_model else []
                contact_tmax = [
                    float(self.basic_model["joints"][index]["motor_limits"]["configured_tmax_nm"])
                    for index in range(6)
                ] if self.basic_model else []
                contact_ratio_caps = [
                    float(value)
                    for value in self.basic_model.get("control", {}).get(
                        "physical_test_pos_tor_ratio_cap",
                        [],
                    )[:6]
                ] if self.basic_model else []
                contact_torque_ceilings = (
                    (np.asarray(contact_tmax) * np.asarray(contact_ratio_caps)).tolist()
                    if len(contact_tmax) == 6 and len(contact_ratio_caps) == 6
                    else []
                )
            except Exception:
                rate_caps, gains = [], []
                contact_tmax, contact_ratio_caps, contact_torque_ceilings = [], [], []
            discovery_ready = bool(
                self.ready
                and self.residency == "HOT"
                and self.health not in {"FAULTED", "UNHEALTHY"}
                and lease is not None
                and str(state.get("health") or "") not in {"FAULTED", "UNHEALTHY"}
                and self._platform_ready_locked()
            )
            capability_readiness = {
                "robot.motion.arm.integrated.mit.one_shot": discovery_ready,
                "robot.motion.arm.integrated.mit.continuous": discovery_ready,
                "robot.motion.arm.integrated.pos_vel.one_shot_limited": discovery_ready,
                "robot.motion.arm.integrated.target_staging": discovery_ready,
                "robot.motion.arm.integrated.runtime_settings": discovery_ready,
                "robot.motion.arm.integrated.semantic_scene_staging": discovery_ready,
                "robot.motion.arm.integrated.nonphysical_preview": discovery_ready,
                "robot.motion.arm.integrated.contact_baseline_capture": discovery_ready,
                "robot.motion.arm.integrated.gripper.mit": discovery_ready,
                "robot.motion.arm.integrated.gripper.pos_tor": discovery_ready,
                "robot.motion.arm.integrated.gravity_float": discovery_ready,
                "robot.motion.arm.integrated.safe_terminate": discovery_ready,
            }
            return {
                "schema": "physical_agent.arm_integrated_mit_bringup_state",
                "schema_version": 4,
                "provider_id": self.config["provider_id"],
                "arm_id": self.config["arm_id"],
                "control_mode": CONTROL_MODE,
                "execution_mode": self.execution_mode,
                "basic_execution_mode": MODE_SPECS[self.execution_mode].basic_mode,
                "interaction_mode": self.interaction_mode,
                "ik_mode": self.ik_mode,
                "residency": self.residency,
                "health": self.health,
                "ready": self.ready,
                "engaged": self.engaged,
                "control_state": self.control_state,
                "fault_reason": self.fault_reason,
                "last_error": self.last_error,
                "basic_connected": bool(state),
                "basic_state": state,
                "capability_readiness": capability_readiness,
                "capability_profiles": {
                    "robot.motion.arm.integrated.mit.one_shot": {
                        "maturity": "USABLE",
                        "execution_mode": PRESS_MIT,
                        "interaction_mode": INTERACTION_ONE_SHOT,
                        "discoverable": True,
                    },
                    "robot.motion.arm.integrated.mit.continuous": {
                        "maturity": "USABLE",
                        "execution_mode": PRESS_MIT,
                        "interaction_mode": INTERACTION_HOLD_LB,
                        "discoverable": True,
                    },
                    "robot.motion.arm.integrated.pos_vel.one_shot_limited": {
                        "maturity": "LIMITED",
                        "execution_mode": TRANSIT_SPEED,
                        "interaction_mode": INTERACTION_ONE_SHOT,
                        "discoverable": True,
                        "constraints": {
                            "maximum_path_length_m": 0.2,
                            "load": "NO_PAYLOAD_OR_HIGH_EXTERNAL_LOAD",
                            "stability_beyond_constraints": "NOT_ESTABLISHED",
                        },
                    },
                },
                "non_discoverable_experiments": {
                    "TRANSIT_SPEED_HOLD_LB": {
                        "maturity": "EXPERIMENTAL_UNSTABLE",
                        "manager_capability_advertised": False,
                    },
                    "CONTACT_WORK_ONE_SHOT_POS_TOR": {
                        "maturity": "EXPERIMENTAL_UNSTABLE",
                        "manager_capability_advertised": False,
                    },
                },
                "input": {
                    "x": self.input.x, "y": self.input.y, "z": self.input.z,
                    "roll": self.input.roll, "pitch": self.input.pitch, "yaw": self.input.yaw,
                    "lb_pressed": self.input.lb_pressed, "age_ms": input_age_ms,
                    "rb_open_pressed": self.input.rb_open_pressed,
                    "rt_close_pressed": self.input.rt_close_pressed,
                },
                "runtime": {
                    "duration_s": self.duration_s,
                    "replan_interval_s": self.replan_interval_s,
                    "kp_multiplier": self.kp_multiplier,
                    "effective_gains": gains,
                    "controlled_frame_offset_xyz_m": self.tool_offset_xyz_m.tolist(),
                    "controlled_frame_offset_rpy_rad": self.tool_offset_rpy_rad.tolist(),
                    "payload_mass_kg": self.payload_mass_kg,
                    "payload_com_tool_m": self.payload_com_tool_m.tolist(),
                    "contact_torque_budget_nm": copy.deepcopy(
                        self.config["contact"]["task_torque_budget_nm"]
                    ),
                    "contact_budget_mode": str(self.config["contact"]["budget_mode"]).upper(),
                    "contact_wrench_force_budget_n": copy.deepcopy(
                        self.config["contact"]["wrench_force_budget_n"]
                    ),
                    "contact_wrench_torque_budget_nm": copy.deepcopy(
                        self.config["contact"]["wrench_torque_budget_nm"]
                    ),
                    "contact_isotropic_force_budget_n": self.config["contact"]["isotropic_force_budget_n"],
                    "contact_isotropic_torque_budget_nm": self.config["contact"]["isotropic_torque_budget_nm"],
                },
                "gripper": {
                    **self._gripper_settings_snapshot_locked(),
                    "hold_to_run": False,
                    "release_behavior": "LATCH_LAST_ENDPOINT",
                    "release_action": "FLOAT_LT_OR_SAFE_TERMINATE",
                    "requires_engage": True,
                    "arm_trajectory_interlock": True,
                    "requested_action": self._desired_gripper_action_locked(),
                    "active_action": self.gripper_active_action,
                    "latched_hold": (
                        self.gripper_active_action is not None
                        and self._desired_gripper_action_locked() is None
                    ),
                    "target_rad": self.gripper_target_rad,
                    "measured_rad": None if positions.size < 7 else float(positions[6]),
                    "velocity_rad_s": None if velocities.size < 7 else float(velocities[6]),
                    "measured_torque_nm": None if torques.size < 7 else float(torques[6]),
                    "command_count": self.gripper_command_count,
                    "stop_count": self.gripper_stop_count,
                    "last_error": self.gripper_last_error,
                    "fault_latched_until_release": self.gripper_fault_latched,
                },
                "external_input": {
                    "update_count": self.external_target_update_count,
                    "last_source": self.last_external_target_source,
                    "last_age_ms": None if not self.last_external_target_monotonic else (time.monotonic() - self.last_external_target_monotonic) * 1000.0,
                    "last_metadata": copy.deepcopy(self.last_external_target_metadata),
                },
                "target": {
                    "staged": self._frame_payload(self.staged_target),
                    "last_commit_origin": self._frame_payload(self.last_commit_origin),
                    "last_committed": self._frame_payload(self.last_committed_target),
                    "last_commit_clamped": self.last_target_clamped,
                    "maximum_commit_distance_m": float(self.config["trajectory"]["maximum_translation_per_commit_m"]),
                    "position_residual_m": self.last_position_residual_m,
                    "orientation_residual_rad": self.last_orientation_residual_rad,
                    "residual_policy": "TELEMETRY_ONLY_NO_EXECUTION_REJECTION",
                    "ik_iterations": self.last_ik_iterations,
                    "sigma_min": self.last_sigma_min,
                },
                "planning": {
                    "target_revision": self.target_revision,
                    "preview_count": self.preview_count,
                    "preview_rejected_count": self.preview_rejected_count,
                    "last_preview": None if self.last_preview is None else {
                        **self.last_preview.snapshot(include_samples=False),
                        **copy.deepcopy(self.last_preview_context or {}),
                    },
                    "scene": None if self.scene is None else self.scene.snapshot(),
                    "scene_source": self.scene_source,
                    "scene_age_ms": None if not self.scene_received_monotonic else (time.monotonic() - self.scene_received_monotonic) * 1000.0,
                },
                "contact_monitoring": {
                    "baseline_state": self.baseline_capture_state,
                    "baseline_error": self.baseline_capture_error,
                    "baseline_age_ms": None if not self.baseline_captured_monotonic else (time.monotonic() - self.baseline_captured_monotonic) * 1000.0,
                    "baseline": None if self.torque_baseline is None else self.torque_baseline.snapshot(),
                    "task_torque_budget_nm": copy.deepcopy(self.config["contact"]["task_torque_budget_nm"]),
                    "budget_mode": str(self.config["contact"]["budget_mode"]).upper(),
                    "wrench_force_budget_n": copy.deepcopy(self.config["contact"]["wrench_force_budget_n"]),
                    "wrench_torque_budget_nm": copy.deepcopy(self.config["contact"]["wrench_torque_budget_nm"]),
                    "isotropic_force_budget_n": self.config["contact"]["isotropic_force_budget_n"],
                    "isotropic_torque_budget_nm": self.config["contact"]["isotropic_torque_budget_nm"],
                    "effective_joint_budget_nm": (
                        None
                        if self.contact_effective_joint_budget_nm is None
                        else self.contact_effective_joint_budget_nm.tolist()
                    ),
                    "torque_limit_ratios": None if self.contact_torque_limit_ratios is None else self.contact_torque_limit_ratios.tolist(),
                    "configured_tmax_nm": contact_tmax,
                    "physical_pos_tor_ratio_caps": contact_ratio_caps,
                    "effective_torque_ceiling_nm": contact_torque_ceilings,
                    "residual_nm": None if self.contact_residual_nm is None else self.contact_residual_nm.tolist(),
                    "limit_violation_joint_indices": list(self.contact_limit_violations),
                    "saturated_joint_indices": list(self.contact_saturated_joint_indices),
                    "saturation_count": self.contact_saturation_count,
                    "saturation_reason": self.contact_saturation_reason,
                    "residual_limit_action": "SATURATE_AFFECTED_JOINTS_AT_PHYSICAL_POS_TOR_CEILING",
                    "automatic_baseline_per_one_shot": False,
                    "baseline_workflow": "MANUAL_CAPTURE_THEN_SEPARATE_CONTACT_WORK_COMMAND",
                    "continuous_contact_work_enabled": False,
                    "completion_policy": "CONFIGURED_DURATION_THEN_FLOAT_WITHOUT_REQUIRING_ARRIVAL",
                    "ratio_margin_rule": "THREE_TIMES_CAPTURED_BASELINE_MAD",
                    "physical_execution_enabled": CONTACT_WORK in {
                        str(value).upper()
                        for value in self.config["safety"]["physically_enabled_execution_modes"]
                    },
                },
                "trajectory": {
                    "active": plan is not None,
                    "execution_mode": None if plan is None else plan.execution_mode,
                    "command_strategy": MODE_SPECS[self.execution_mode].command_strategy,
                    "interaction_mode": None if plan is None else plan.interaction_mode,
                    "ik_mode": None if plan is None else plan.ik_mode,
                    "segment_duration_s": None if plan is None else plan.duration_s,
                    "progress": progress,
                    "replan_count": None if plan is None else plan.replan_count,
                    "target_revision": None if plan is None else plan.target_revision,
                    "frames_attempted": None if plan is None else plan.frames_attempted,
                    "frames_sent": None if plan is None else plan.frames_sent,
                    "frames_skipped": None if plan is None else plan.frames_skipped,
                    "send_rate_hz": float(self.config["trajectory"]["send_rate_hz"]),
                    "max_send_lateness_ms": self.max_send_lateness_ms,
                    "last_replan_duration_ms": self.last_replan_duration_ms,
                    "max_replan_duration_ms": self.max_replan_duration_ms,
                    "last_completed": copy.deepcopy(self.last_completed_trajectory),
                    "latched_endpoint": None if self.latched_endpoint is None else self.latched_endpoint.snapshot(),
                    "hybrid": None if self.hybrid_policy is None else self.hybrid_policy.snapshot(),
                },
                "joint_state": {
                    "measured_rad": measured_q,
                    "commanded_rad": None if self.commanded_q is None else self.commanded_q.tolist(),
                    "commanded_velocity_rad_s": None if self.commanded_qd is None else self.commanded_qd.tolist(),
                    "goal_rad": None if self.goal_q is None else self.goal_q.tolist(),
                    "velocity_rad_s": velocity_q,
                    "measured_torque_nm": torque_q,
                    "tracking_error_rad": self.last_tracking_error_rad,
                    "max_tracking_error_rad": self.max_tracking_error_rad,
                    "provider_rate_caps_rad_s": rate_caps,
                },
                "model_view": {
                    "measured_points_m": measured_points,
                    "commanded_points_m": commanded_points,
                    "measured_tool": self._frame_payload(measured_tool),
                    "measured_controlled_frame": self._frame_payload(measured_control),
                    "staged_controlled_frame": self._frame_payload(self.staged_target),
                    "commanded_controlled_frame": self._frame_payload(commanded_control),
                    "goal_controlled_frame": self._frame_payload(goal_control),
                },
                "gravity_support": {
                    "basic_total_nm": copy.deepcopy(state.get("gravity_compensation", {}).get("total_nm")),
                    "basic_payload_nm": copy.deepcopy(state.get("gravity_compensation", {}).get("payload_nm")),
                    "clamped_to_motor_tmax": copy.deepcopy(state.get("gravity_compensation", {}).get("clamped_to_motor_tmax")),
                    "commanded_height_error_m": None if measured_control is None or commanded_control is None else float(commanded_control[2, 3] - measured_control[2, 3]),
                    "goal_height_error_m": None if measured_control is None or goal_control is None else float(goal_control[2, 3] - measured_control[2, 3]),
                    "feedforward_owner": "BASIC_ARM_PLUS_PAYLOAD_MODEL",
                },
                "lease": {
                    "active": lease is not None,
                    "state": self.lease_state,
                    "lease_id": None if lease is None else lease.lease_id,
                    "fencing_generation": None if lease is None else lease.fencing_generation,
                    "expires_in_ms": None if lease is None else max(0, int((lease.expires_monotonic - time.monotonic()) * 1000.0)),
                    "renew_success_count": self.lease_renew_success_count,
                    "renew_failure_count": self.lease_renew_failure_count,
                    "last_renew_latency_ms": self.lease_renew_latency_ms,
                },
                "safety": {
                    "float_confirmed": self.float_confirmed,
                    "float_request_count": self.float_request_count,
                    "float_failure_count": self.float_failure_count,
                    "last_float_confirmed_age_ms": float_age_ms,
                    "last_float_reason": self.last_float_reason,
                    "manager_registered": self.manager_registered,
                    "fabric_ready": self.fabric_ready,
                    "platform_ready": self._platform_ready_locked(),
                    "motion_inhibited": self.motion_inhibited,
                    "motion_inhibit_owners": copy.deepcopy(self.motion_inhibit_owners),
                    "payload_gravity_compensation": "BASIC_FENCED_PAYLOAD_MODEL",
                    "physically_enabled_execution_modes": list(self.config["safety"]["physically_enabled_execution_modes"]),
                    "small_force_is_not_assumed_safe": True,
                },
                "command_count": self.command_count,
                "commit_count": self.commit_count,
                "live_replan_count": self.live_replan_count,
                "rejected_count": self.rejected_count,
                "last_command_latency_ms": self.last_command_latency_ms,
                "max_command_latency_ms": self.max_command_latency_ms,
                "platform_errors": copy.deepcopy(self.platform_errors),
                "units": {
                    "joint_position": "rad", "joint_velocity": "rad/s", "joint_torque": "Nm",
                    "cartesian_position": "m", "cartesian_orientation": "rad",
                },
            }
