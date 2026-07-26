"""Local control loop and safety-state machine."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable
import copy
import math
import threading
import time
import uuid

import numpy as np

from .dynamics import RebotDynamics
from .hardware import HardwareBackend, JointFeedback
from .models import ArmConfiguration


class ProviderState(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    READ_ONLY = "READ_ONLY"
    CALIBRATION_MANUAL = "CALIBRATION_MANUAL"
    TRAJECTORY_CONTROL = "TRAJECTORY_CONTROL"
    SAFE_HOLD_GRAVITY_FLOAT = "SAFE_HOLD_GRAVITY_FLOAT"
    CALIBRATION_POSITION_HOLD = "CALIBRATION_POSITION_HOLD"
    SAFE_HOME = "SAFE_HOME"
    FAULTED = "FAULTED"
    EMERGENCY_DISABLED = "EMERGENCY_DISABLED"


@dataclass
class ControlLease:
    lease_id: str
    fencing_generation: int
    holder: str
    expires_monotonic: float

    def valid(self, lease_id: str, generation: int, now: float) -> bool:
        return self.lease_id == lease_id and self.fencing_generation == generation and now < self.expires_monotonic


class LeasePermissionError(PermissionError):
    def __init__(self, error_code: str, reason: str, lease_status: dict[str, Any] | None = None):
        super().__init__(reason)
        self.error_code = error_code
        self.reason = reason
        self.lease_status = lease_status or {}


@dataclass
class JointCommand:
    mode: str
    values: dict[str, float]


@dataclass
class CommandEnvelope:
    command_id: str
    lease_id: str
    fencing_generation: int
    commands: dict[int, JointCommand]
    deadline_monotonic: float
    created_monotonic: float = field(default_factory=time.monotonic)


class ArmController:
    """Runs all motor writes and immediate safety transitions in one local loop."""

    def __init__(self, configuration: ArmConfiguration, backend: HardwareBackend, dynamics: RebotDynamics):
        self.configuration = configuration
        self.backend = backend
        self.dynamics = dynamics
        self.rate_hz = float(configuration.model["control"]["internal_rate_hz"])
        self.period = 1.0 / self.rate_hz
        self.lock = threading.RLock()
        # Fast ingress state is intentionally separate from the hardware/control lock.
        # HTTP lease renewals and command submissions must never wait for serial I/O.
        self.ingress_lock = threading.RLock()
        # State delivery must remain responsive when a driver call holds the
        # control lock until the operating system reports a serial timeout.
        self.snapshot_cache_lock = threading.Lock()
        self.snapshot_cache: dict[str, Any] = {
            "state": ProviderState.DISCONNECTED.value,
            "health": "HEALTHY",
            "ready": False,
        }
        self.snapshot_cache_monotonic = time.monotonic()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.state = ProviderState.DISCONNECTED
        self.health = "HEALTHY"
        self.last_error: str | None = None
        self.feedback: JointFeedback | None = None
        self.lease: ControlLease | None = None
        self.fencing_generation = 0
        self.pending: CommandEnvelope | None = None
        self.last_applied_command_id: str | None = None
        self.last_valid_command_monotonic = 0.0
        self.command_submission_count = 0
        self.pending_replacement_count = 0
        self.last_submitted_command_id: str | None = None
        self.last_replaced_command_id: str | None = None
        self.last_submitted_modes: list[str] = []
        self.hold_reference = configuration.home_positions.copy()
        # Persistent MIT moving target, equivalent to the supplied Unity bridge q_mt.
        self.mit_moving_target = configuration.home_positions.copy()
        self.active_command_modes: list[str | None] = [None] * 7
        self.float_entry_started = 0.0
        self.float_start_gravity = np.zeros(7)
        self.last_float_reason: str | None = None
        self.last_position_hold_reason: str | None = None
        self.position_hold_velocity_limit = 0.12
        self.loop_count = 0
        # missed_deadlines is retained for API compatibility and now means the
        # number of skipped 10 ms schedule slots, not catch-up loop iterations.
        self.missed_deadlines = 0
        self.deadline_overrun_events = 0
        self.max_loop_lateness_ms = 0.0
        self.last_loop_lateness_ms = 0.0
        self.last_tick_duration_ms = 0.0
        self.max_tick_duration_ms = 0.0
        self.on_sample: Callable[[dict[str, Any]], None] | None = None
        self.graceful_shutdown_complete = threading.Event()
        self.lease_event_sequence = 0
        self.last_lease_event: dict[str, Any] = {
            "sequence": 0, "event": "NONE", "reason": "no lease event recorded", "observed_at_us": time.time_ns()//1000
        }
        self.last_lease_drop_reason: str | None = None
        self.last_gravity_compensation_nm = np.zeros(7, dtype=float)
        self.last_payload_gravity_nm = np.zeros(7, dtype=float)
        self.gravity_compensation_clamped = [False] * 7
        self.mode_transition_signature: tuple[str, ...] | None = None
        self.mode_transition_hold_reference: np.ndarray | None = None
        self.mode_transition_step_count = 0
        self.mode_transition_failure_count = 0
        self.last_mode_transition_joint: int | None = None
        self.last_mode_transition_from: str | None = None
        self.last_mode_transition_to: str | None = None
        self.control_fault_count = 0
        self.last_control_fault_at_us: int | None = None
        self.endpoint_keepalive_period_s = 1.0 / float(
            configuration.model["control"]["motor_endpoint_keepalive_hz"]
        )
        self.latched_endpoint_signatures: list[tuple[Any, ...] | None] = [None] * 7
        self.latched_endpoint_last_sent_monotonic = np.zeros(7, dtype=float)
        self.latched_endpoint_frames_sent = 0
        self.latched_endpoint_frames_suppressed = 0


    def set_payload(self, lease_id: str, generation: int, mass_kg: float, com_tool_m: Any) -> dict[str, Any]:
        """Update the tool payload under the same fenced operational lease as motion."""
        now = time.monotonic()
        with self.ingress_lock:
            current = self.lease
            if current is None:
                raise LeasePermissionError("NO_ACTIVE_LEASE", "payload update requires an active operational lease", self._lease_status_ingress_locked())
            if now >= current.expires_monotonic:
                raise LeasePermissionError("LEASE_EXPIRED", "payload update used an expired lease", self._lease_status_ingress_locked())
            if current.lease_id != lease_id or current.fencing_generation != generation:
                raise LeasePermissionError("STALE_LEASE", "payload update used a stale lease", self._lease_status_ingress_locked())
        with self.lock:
            self.dynamics.set_payload(mass_kg, com_tool_m)
            payload = self.dynamics.payload_snapshot()
        return payload

    def _gravity_with_payload_locked(self, positions_rad: Any) -> np.ndarray:
        """Apply payload compensation while retaining per-motor torque hard limits."""
        arm = self.dynamics.calibrated_gravity_torque(positions_rad)
        payload = self.dynamics.payload_gravity_torque(positions_rad)
        raw = arm + payload
        clipped = raw.copy()
        flags: list[bool] = []
        for index, joint in enumerate(self.configuration.joints):
            if index >= 6:
                clipped[index] = 0.0
                flags.append(False)
                continue
            limit = abs(float(joint.configured_tmax_nm))
            bounded = float(np.clip(raw[index], -limit, limit))
            flags.append(abs(bounded - float(raw[index])) > 1e-9)
            clipped[index] = bounded
        self.last_gravity_compensation_nm = clipped.copy()
        self.last_payload_gravity_nm = payload.copy()
        self.gravity_compensation_clamped = flags
        return clipped

    def start(self) -> None:
        with self.lock:
            if self.thread and self.thread.is_alive(): return
            self.backend.connect(); self.feedback = self.backend.read()
            self.hold_reference = self.feedback.positions_rad.copy()
            self.mit_moving_target = self.feedback.positions_rad.copy()
            self.active_command_modes = [None] * 7
            self.state = ProviderState.READ_ONLY
            self.stop_event.clear(); self.graceful_shutdown_complete.clear()
            self.thread = threading.Thread(target=self._run, name="rebot-arm-control", daemon=True)
            self.thread.start()

    def enable(self) -> None:
        with self.lock:
            if self.state == ProviderState.DISCONNECTED: raise RuntimeError("provider is disconnected")
            self.backend.enable()
            if self.feedback is not None:
                self.hold_reference = self.feedback.positions_rad.copy()
                self.mit_moving_target = self.feedback.positions_rad.copy()
            self._enter_gravity_float_locked("motor enable", immediate=True, apply_now=True)

    def _lease_status_ingress_locked(self) -> dict[str, Any]:
        now = time.monotonic()
        if self.lease is None:
            return {"active": False, "current_holder": None, "current_generation": None, "expires_in_ms": None}
        return {
            "active": now < self.lease.expires_monotonic,
            "current_holder": self.lease.holder,
            "current_generation": self.lease.fencing_generation,
            "expires_in_ms": int((self.lease.expires_monotonic-now)*1000),
        }

    def _record_lease_event_ingress_locked(self, event: str, reason: str, *, lease: ControlLease | None = None, requested_lease_id: str | None = None, requested_generation: int | None = None) -> None:
        self.lease_event_sequence += 1
        source = lease or self.lease
        self.last_lease_event = {
            "sequence": self.lease_event_sequence,
            "event": event,
            "reason": reason,
            "observed_at_us": time.time_ns()//1000,
            "holder": None if source is None else source.holder,
            "lease_id": None if source is None else source.lease_id,
            "fencing_generation": None if source is None else source.fencing_generation,
            "requested_lease_id": requested_lease_id,
            "requested_generation": requested_generation,
        }
        if event in {"EXPIRED", "RELEASED", "REVOKED", "REPLACED", "RENEW_REJECTED", "COMMAND_REJECTED"}:
            self.last_lease_drop_reason = reason
        print(f"[basic-lease] event={event} reason={reason} holder={self.last_lease_event['holder']} generation={self.last_lease_event['fencing_generation']}")

    def acquire_lease(self, holder: str, duration_ms: int | None = None) -> ControlLease:
        """Acquire exclusive control without silently replacing a valid owner."""
        timeout = (
            duration_ms
            or int(self.configuration.model["control"]["lease_timeout_ms"])
        ) / 1000.0
        expired: ControlLease | None = None
        with self.ingress_lock:
            now = time.monotonic()
            current = self.lease
            if current is not None and now < current.expires_monotonic:
                reason = (
                    f"control is already leased to {current.holder}; "
                    f"generation {current.fencing_generation} has "
                    f"{max(0, int((current.expires_monotonic-now)*1000))} ms remaining"
                )
                self._record_lease_event_ingress_locked(
                    "ACQUIRE_REJECTED", reason, lease=current
                )
                raise LeasePermissionError(
                    "ACTIVE_LEASE_CONFLICT", reason, self._lease_status_ingress_locked()
                )
            if current is not None:
                expired = current
                self.lease = None
                self.pending = None
                self._record_lease_event_ingress_locked(
                    "EXPIRED", "expired lease cleared during acquisition", lease=current
                )
            # Allocate while the ingress lock is still held. Otherwise two
            # simultaneous acquisition requests can both observe an empty slot
            # and the later request can silently replace the first one.
            self.fencing_generation += 1
            lease = ControlLease(
                str(uuid.uuid4()), self.fencing_generation, holder, now + timeout
            )
            self.lease = lease
            self._record_lease_event_ingress_locked(
                "ACQUIRED", f"lease acquired for {int(timeout*1000)} ms", lease=lease
            )
        if expired is not None:
            with self.lock:
                self._enter_gravity_float_locked(
                    "expired lease cleared before acquisition",
                    immediate=True,
                    apply_now=True,
                )
        return lease

    def renew_lease(self, lease_id: str, generation: int, duration_ms: int | None = None) -> ControlLease:
        now=time.monotonic(); timeout=(duration_ms or int(self.configuration.model["control"]["lease_timeout_ms"]))/1000.0
        expired_reason=None
        with self.ingress_lock:
            if self.lease is None:
                reason=self.last_lease_drop_reason or "no active lease exists"
                self._record_lease_event_ingress_locked("RENEW_REJECTED",reason,requested_lease_id=lease_id,requested_generation=generation)
                raise LeasePermissionError("NO_ACTIVE_LEASE",reason,self._lease_status_ingress_locked())
            current=self.lease
            if now>=current.expires_monotonic:
                overdue_ms=int((now-current.expires_monotonic)*1000)
                expired_reason=f"lease expired {overdue_ms} ms ago for holder {current.holder}"
                self._record_lease_event_ingress_locked("EXPIRED",expired_reason,lease=current,requested_lease_id=lease_id,requested_generation=generation)
                self.lease=None; self.pending=None
            elif current.lease_id != lease_id:
                reason=f"lease id does not match active holder {current.holder}; active generation is {current.fencing_generation}"
                self._record_lease_event_ingress_locked("RENEW_REJECTED",reason,lease=current,requested_lease_id=lease_id,requested_generation=generation)
                raise LeasePermissionError("LEASE_ID_MISMATCH",reason,self._lease_status_ingress_locked())
            elif current.fencing_generation != generation:
                reason=f"stale fencing generation {generation}; active generation is {current.fencing_generation} for holder {current.holder}"
                self._record_lease_event_ingress_locked("RENEW_REJECTED",reason,lease=current,requested_lease_id=lease_id,requested_generation=generation)
                raise LeasePermissionError("STALE_FENCING_GENERATION",reason,self._lease_status_ingress_locked())
            else:
                current.expires_monotonic=now+timeout
                return current
        if expired_reason is not None:
            with self.lock:
                self._enter_gravity_float_locked("lease expired", immediate=True, apply_now=True)
            raise LeasePermissionError("LEASE_EXPIRED",expired_reason,{"active":False,"current_holder":None,"current_generation":None,"expires_in_ms":None})
        raise RuntimeError("unreachable lease renewal state")

    def release_lease(self, lease_id: str, generation: int, *, fallback_to_float: bool = True) -> None:
        """Release only the exact fenced lease supplied by its owner."""
        with self.ingress_lock:
            current = self.lease
            if current is None:
                raise LeasePermissionError(
                    "NO_ACTIVE_LEASE",
                    "release rejected because no active lease exists",
                    self._lease_status_ingress_locked(),
                )
            if current.lease_id != lease_id or current.fencing_generation != generation:
                reason = (
                    f"release used a stale lease; active holder is {current.holder}, "
                    f"generation {current.fencing_generation}"
                )
                self._record_lease_event_ingress_locked(
                    "RELEASE_REJECTED", reason, lease=current,
                    requested_lease_id=lease_id, requested_generation=generation
                )
                raise LeasePermissionError(
                    "STALE_LEASE", reason, self._lease_status_ingress_locked()
                )
            released = current
            self.lease = None
            self.pending = None
            self.fencing_generation += 1
            self._record_lease_event_ingress_locked(
                "RELEASED", "lease released by holder", lease=released,
                requested_lease_id=lease_id, requested_generation=generation
            )
        if fallback_to_float:
            with self.lock:
                self._enter_gravity_float_locked("lease released", immediate=True, apply_now=True)

    def request_position_hold(self, reason: str = "requested", positions: np.ndarray | list[float] | None = None, velocity_limit: float = 0.12) -> None:
        """Hold every joint using the motor-side position/velocity mode."""
        with self.ingress_lock:
            self.pending=None
        with self.lock:
            if self.feedback is None:
                raise RuntimeError("joint feedback is unavailable")
            requested = self.feedback.positions_rad if positions is None else np.asarray(positions, dtype=float)
            if requested.shape != (7,) or not np.all(np.isfinite(requested)):
                raise ValueError("position hold requires seven finite joint angles")
            for index, value in enumerate(requested):
                limits = self.configuration.operational_limits[index]
                if value < limits[0] or value > limits[1]:
                    raise ValueError(f"position hold joint {index+1} is outside the operational range")
            self.hold_reference = requested.copy()
            self.position_hold_velocity_limit = float(np.clip(velocity_limit, 0.02, 0.5))
            self.state = ProviderState.CALIBRATION_POSITION_HOLD
            self.last_position_hold_reason = reason
            self.last_error = None if self.health == "HEALTHY" else self.last_error
            self._apply_position_hold_locked()

    def submit(self, envelope: CommandEnvelope) -> None:
        # Validate the lease and publish the newest command through the fast ingress
        # lock. This path deliberately does not acquire the hardware/control lock.
        now=time.monotonic()
        with self.ingress_lock:
            current=self.lease
            if current is None:
                reason=self.last_lease_drop_reason or "command rejected because no active lease exists"
                self._record_lease_event_ingress_locked("COMMAND_REJECTED",reason,requested_lease_id=envelope.lease_id,requested_generation=envelope.fencing_generation)
                raise LeasePermissionError("NO_ACTIVE_LEASE",reason,self._lease_status_ingress_locked())
            if now>=current.expires_monotonic:
                reason=f"command rejected because lease for {current.holder} expired"
                self._record_lease_event_ingress_locked("COMMAND_REJECTED",reason,lease=current,requested_lease_id=envelope.lease_id,requested_generation=envelope.fencing_generation)
                self.lease=None; self.pending=None
                expired=True
            else:
                expired=False
                if current.lease_id != envelope.lease_id or current.fencing_generation != envelope.fencing_generation:
                    reason=f"command used stale lease; active holder is {current.holder}, generation {current.fencing_generation}"
                    self._record_lease_event_ingress_locked("COMMAND_REJECTED",reason,lease=current,requested_lease_id=envelope.lease_id,requested_generation=envelope.fencing_generation)
                    raise LeasePermissionError("STALE_LEASE",reason,self._lease_status_ingress_locked())
        if expired:
            with self.lock:
                self._enter_gravity_float_locked("lease expired", immediate=True, apply_now=True)
            raise LeasePermissionError("LEASE_EXPIRED",reason,{"active":False,"current_holder":None,"current_generation":None,"expires_in_ms":None})
        if envelope.deadline_monotonic <= now:
            raise TimeoutError("command deadline has expired")
        state=self.state
        if state in {
            ProviderState.SAFE_HOME,
            ProviderState.FAULTED,
            ProviderState.EMERGENCY_DISABLED,
            ProviderState.DISCONNECTED,
        }:
            raise RuntimeError(f"commands are blocked in {state.value}")
        validated={}
        for index, command in envelope.commands.items():
            validated[int(index)] = JointCommand(command.mode,self.configuration.validate_joint_command(int(index),command.mode,command.values))
        envelope.commands=validated
        with self.ingress_lock:
            current=self.lease
            if current is None or current.lease_id != envelope.lease_id or current.fencing_generation != envelope.fencing_generation:
                raise LeasePermissionError("STALE_LEASE","lease changed while command was validated",self._lease_status_ingress_locked())
            if envelope.deadline_monotonic <= time.monotonic():
                raise TimeoutError("command deadline has expired")
            previous=self.pending
            if previous is not None and previous.command_id != envelope.command_id:
                self.pending_replacement_count+=1
                self.last_replaced_command_id=previous.command_id
            self.pending=envelope
            self.command_submission_count+=1
            self.last_submitted_command_id=envelope.command_id
            self.last_submitted_modes=sorted({command.mode for command in envelope.commands.values()})
            self.last_valid_command_monotonic=time.monotonic()

    def request_gravity_float(self, reason: str = "requested") -> None:
        with self.ingress_lock:
            self.pending=None
        with self.lock:
            self._enter_gravity_float_locked(reason, immediate=True, apply_now=True)

    def revoke_lease(self, reason: str = "lease revoked") -> None:
        """Fence the current controller and immediately enter gravity-float."""
        with self.ingress_lock:
            revoked=self.lease
            self.lease=None; self.pending=None; self.fencing_generation += 1
            self._record_lease_event_ingress_locked("REVOKED",reason,lease=revoked)
        with self.lock:
            self._enter_gravity_float_locked(reason, immediate=True, apply_now=True)

    def _enter_gravity_float_locked(self, reason: str, *, immediate: bool = True, apply_now: bool = False) -> None:
        if self.feedback is not None:
            # Stop any motor-side motion before changing back to MIT. A POS_VEL
            # or FORCE_POS motor can otherwise keep travelling toward its old
            # target while mode switching is in progress.
            self._freeze_active_motor_modes_locked()
            self.hold_reference=self.feedback.positions_rad.copy()
            self.mit_moving_target=self.feedback.positions_rad.copy()
            desired=self._gravity_with_payload_locked(self.feedback.positions_rad)
            self.float_start_gravity=desired.copy() if immediate else np.zeros(7)
        self.mode_transition_signature=None
        self.mode_transition_hold_reference=None
        self.latched_endpoint_signatures=[None]*7
        self.latched_endpoint_last_sent_monotonic.fill(0.0)
        self.float_entry_started=time.monotonic()
        self.state=ProviderState.SAFE_HOLD_GRAVITY_FLOAT
        self.last_float_reason=reason
        self.last_error=None if self.health == "HEALTHY" else self.last_error
        if apply_now and self.feedback is not None:
            self._apply_gravity_float_locked(time.monotonic())

    def _freeze_active_motor_modes_locked(self) -> None:
        """Cancel motor-side motion at the latest measured position.

        This is intentionally performed synchronously during a deadman/lease
        release, before any potentially slower mode transitions.
        """
        if self.feedback is None:
            return
        for index, mode in enumerate(self.active_command_modes):
            if mode is None or mode == "IMPEDANCE":
                continue
            position=float(self.feedback.positions_rad[index])
            velocity_limit=min(0.12, max(0.02, float(self.configuration.joints[index].default_vlim)))
            if mode == "POSITION_VELOCITY_LIMITED":
                self.backend.send_position_velocity(index, position, velocity_limit)
            elif mode == "POSITION_EFFORT_LIMITED":
                ratio=float(self.configuration.model["joints"][index]["default_test"].get("torque_limit_ratio",0.12))
                self.backend.send_force_position(index, position, velocity_limit, ratio)
            elif mode == "VELOCITY":
                self.backend.send_velocity(index, 0.0)


    def emergency_disable(self, reason: str) -> None:
        with self.ingress_lock:
            self.pending=None
        with self.lock:
            self.backend.disable(); self.state=ProviderState.EMERGENCY_DISABLED; self.health="FAULTED"; self.last_error=reason

    def _safe_home_gains_locked(self) -> tuple[np.ndarray, np.ndarray]:
        """Return safe-home gains with the load-bearing spring floor enforced.

        The model may retain historical safe-home values, but safe-home is a
        load-bearing MIT state.  Its kp must therefore never be below the same
        reviewed floor used by gravity-float and manual MIT control.  kd remains
        independent and may be comparatively low.
        """
        configured_kp=np.asarray(self.configuration.model["control"]["safe_home_kp"],dtype=float)
        configured_kd=np.asarray(self.configuration.model["control"]["safe_home_kd"],dtype=float)
        floors=np.asarray([
            max(
                float(self.configuration.model["joints"][index].get("provider_test_caps",{}).get("min_kp",joint.default_kp)),
                float(self.configuration.calibration_by_name[joint.name]["safe_float_kp"]),
            )
            for index,joint in enumerate(self.configuration.joints)
        ],dtype=float)
        return np.maximum(configured_kp,floors),np.maximum(configured_kd,0.0)

    def _send_supported_mit_target_locked(self, target: np.ndarray, kp: np.ndarray, kd: np.ndarray) -> None:
        """Send one fully supported MIT frame for all joints."""
        assert self.feedback is not None
        gravity=self._gravity_with_payload_locked(self.feedback.positions_rad)
        for index in range(7):
            feedforward=float(gravity[index]) if index < 6 else 0.0
            self.backend.send_impedance(
                index,float(target[index]),0.0,float(kp[index]),float(kd[index]),feedforward
            )
            self.active_command_modes[index]="IMPEDANCE"
            self.mit_moving_target[index]=float(target[index])

    def safe_home(self, timeout_s: float | None = None) -> bool:
        """Rate-limit all MIT targets toward the configured zero/home vector.

        This mirrors the official Python and supplied Unity bridge behavior:
        the active powered MIT loop moves toward home and verifies measured
        joint position before shutdown.
        """
        timeout=float(timeout_s or self.configuration.model["control"]["safe_home_timeout_s"])
        tolerance=float(self.configuration.model["home"]["tolerance_rad"])
        max_velocity=float(self.configuration.model["control"]["safe_home_max_velocity_rad_s"])
        settle_velocity=float(self.configuration.model["control"].get("safe_home_settle_velocity_rad_s",0.06))
        settle_cycles=int(self.configuration.model["control"].get("safe_home_settle_cycles",10))
        transition_hold_cycles=max(1,int(self.configuration.model["control"].get("safe_home_transition_hold_cycles",5)))
        home=self.configuration.home_positions
        with self.ingress_lock:
            active_lease=self.lease
            self.lease=None
            self.pending=None
            if active_lease is not None:
                self.fencing_generation += 1
                self._record_lease_event_ingress_locked(
                    "REVOKED",
                    "safe-home preempted operational control",
                    lease=active_lease,
                )
        with self.lock:
            if self.feedback is None: return False
            if self.state == ProviderState.READ_ONLY:
                self.backend.enable()
            kp,kd=self._safe_home_gains_locked()
            target=self.feedback.positions_rad.copy()
            self.hold_reference=target.copy()
            self.state=ProviderState.SAFE_HOME
            # Seamless handoff: the first safe-home command captures the measured
            # position with full load-bearing stiffness and gravity support.  The
            # home target is not advanced until this supported frame has been sent.
            self._send_supported_mit_target_locked(target,kp,kd)
            already_home=bool(np.all(np.abs(self.feedback.positions_rad-home)<=tolerance))
        if already_home:
            with self.lock:
                self._enter_gravity_float_locked("already at safe-home", immediate=True, apply_now=True)
            return True
        # Keep the captured target powered for several control periods while any
        # motor-side mode completes its transition into MIT.
        for _ in range(transition_hold_cycles):
            if self.stop_event.is_set(): break
            time.sleep(self.period)
            with self.lock:
                if self.feedback is None: break
                self._send_supported_mit_target_locked(target,kp,kd)
        deadline=time.monotonic()+timeout; settled=0; max_step=max_velocity*self.period
        while time.monotonic()<deadline and not self.stop_event.is_set():
            with self.lock:
                feedback=self.feedback
                if feedback is None: break
                error=home-target
                target += np.clip(error,-max_step,max_step)
                self._send_supported_mit_target_locked(target,kp,kd)
                position_ok=bool(np.all(np.abs(feedback.positions_rad-home)<=tolerance))
                velocity_ok=bool(np.all(np.abs(feedback.velocities_rad_s)<=settle_velocity))
                settled=settled+1 if position_ok and velocity_ok else 0
                if settled>=settle_cycles:
                    self._enter_gravity_float_locked("safe-home complete", immediate=True, apply_now=True)
                    return True
            time.sleep(self.period)
        with self.lock: self._enter_gravity_float_locked("safe-home incomplete", immediate=True, apply_now=True)
        return False

    def enter_warm(self) -> bool:
        """Safe-home, stop the loop, disable motors, and release the device."""
        with self.lock:
            if self.state == ProviderState.DISCONNECTED:
                return True
        with self.ingress_lock:
            self.pending=None
        homed=self.safe_home()
        if not homed:
            with self.lock:
                self.health="DEGRADED"
                self.last_error="warm transition could not complete safe-home; gravity-float retained"
            return False
        # Keep the powered high-kp gravity-supported home state alive briefly
        # before stopping the control thread.  File/network shutdown work must not
        # create an unsupported gap between safe-home and motor disable.
        powered_settle_s=max(0.0,float(self.configuration.model["control"].get("safe_home_powered_settle_s",0.25)))
        settle_deadline=time.monotonic()+powered_settle_s
        while time.monotonic()<settle_deadline and not self.stop_event.is_set():
            time.sleep(min(self.period,max(0.0,settle_deadline-time.monotonic())))
        with self.lock:
            if self.feedback is not None:
                self._apply_gravity_float_locked(time.monotonic())
        self.stop_event.set()
        if self.thread and self.thread.is_alive() and self.thread is not threading.current_thread():
            self.thread.join(timeout=2.0)
        with self.lock:
            self.backend.disable()
            self.backend.disconnect()
            self.state=ProviderState.DISCONNECTED
            self.feedback=None
        with self.ingress_lock:
            self.lease=None
            self.pending=None
        return True

    def graceful_stop(self) -> bool:
        complete=self.enter_warm()
        if complete:
            self.graceful_shutdown_complete.set()
        return complete

    def close(self, force: bool = False) -> bool:
        if force:
            self.stop_event.set()
            if self.thread and self.thread.is_alive() and self.thread is not threading.current_thread():
                self.thread.join(timeout=2.0)
            with self.lock:
                try: self.backend.disable()
                finally:
                    self.backend.disconnect(); self.state=ProviderState.DISCONNECTED; self.feedback=None
            return True
        return self.graceful_stop()

    def _account_schedule_lateness(self, now: float, next_tick: float) -> float:
        """Skip missed schedule slots instead of issuing catch-up write bursts."""
        lateness=max(0.0,now-next_tick)
        self.last_loop_lateness_ms=lateness*1000.0
        self.max_loop_lateness_ms=max(self.max_loop_lateness_ms,self.last_loop_lateness_ms)
        if lateness>self.period:
            skipped=max(1,int(lateness//self.period))
            self.deadline_overrun_events+=1
            self.missed_deadlines+=skipped
            next_tick+=skipped*self.period
        return next_tick

    def _run(self) -> None:
        next_tick=time.monotonic()
        while not self.stop_event.is_set():
            now=time.monotonic()
            next_tick=self._account_schedule_lateness(now,next_tick)
            tick_started=time.monotonic()
            try: self._tick(now)
            except Exception as error:
                with self.ingress_lock:
                    self.pending=None
                with self.lock:
                    self.health="FAULTED"
                    self.last_error=str(error)
                    self.control_fault_count += 1
                    self.last_control_fault_at_us = time.time_ns() // 1000
                    self._update_snapshot_cache_locked()
                    diagnostics = self.backend.diagnostics() if hasattr(self.backend, "diagnostics") else {}
                    print(
                        f"[basic-control-fault] count={self.control_fault_count} "
                        f"state={self.state.value} error={error} hardware_io={diagnostics}"
                    )
                    try:
                        self._enter_gravity_float_locked("control loop exception", immediate=True, apply_now=True)
                    except Exception as fallback_error:
                        self.state=ProviderState.FAULTED
                        print(f"[basic-control-fault] gravity-float fallback failed: {fallback_error}")
                    self._update_snapshot_cache_locked()
            finally:
                tick_duration_ms=(time.monotonic()-tick_started)*1000.0
                self.last_tick_duration_ms=tick_duration_ms
                self.max_tick_duration_ms=max(self.max_tick_duration_ms,tick_duration_ms)
            self.loop_count+=1; next_tick+=self.period
            delay=next_tick-time.monotonic()
            if delay>0: time.sleep(delay)
            elif delay < -0.25: next_tick=time.monotonic()

    def _tick(self, now: float) -> None:
        lease_expired_reason=None
        command_expired=False
        pending=None
        no_active_lease=False
        with self.ingress_lock:
            if self.lease is not None and now>=self.lease.expires_monotonic:
                expired=self.lease
                overdue_ms=int((now-expired.expires_monotonic)*1000)
                lease_expired_reason=f"lease expired {overdue_ms} ms ago for holder {expired.holder}"
                self._record_lease_event_ingress_locked("EXPIRED",lease_expired_reason,lease=expired)
                self.lease=None; self.pending=None
            elif self.pending is not None and now>=self.pending.deadline_monotonic:
                self.pending=None; command_expired=True
            pending=self.pending
            no_active_lease=self.lease is None

        with self.lock:
            self.feedback=self.backend.read()
            if self.feedback is None:
                return
            if lease_expired_reason is not None:
                self._enter_gravity_float_locked("lease expired", immediate=True, apply_now=True)
            elif command_expired:
                self._enter_gravity_float_locked("command expired", immediate=True, apply_now=True)
            elif pending is not None:
                self.state=ProviderState.CALIBRATION_MANUAL
                self._apply_pending_locked(pending)
            elif no_active_lease and self.state not in {
                ProviderState.READ_ONLY, ProviderState.SAFE_HOLD_GRAVITY_FLOAT,
                ProviderState.SAFE_HOME, ProviderState.DISCONNECTED,
                ProviderState.EMERGENCY_DISABLED, ProviderState.FAULTED,
            }:
                self._enter_gravity_float_locked("no active lease", immediate=True, apply_now=True)
            elif self.state == ProviderState.SAFE_HOLD_GRAVITY_FLOAT:
                self._apply_gravity_float_locked(now)
            elif self.state == ProviderState.CALIBRATION_POSITION_HOLD:
                self._apply_position_hold_locked()
            sample=self.snapshot_locked()
            self._store_snapshot_cache(sample)
        if self.on_sample is not None:
            self.on_sample(sample)

    def _apply_pending_locked(self, envelope: CommandEnvelope) -> None:
        assert self.feedback is not None
        gravity=self._gravity_with_payload_locked(self.feedback.positions_rad)
        if self._apply_mode_transition_frame_locked(envelope, gravity):
            return
        # Every joint not explicitly commanded remains in high-kp measured-target gravity support.
        for index,joint in enumerate(self.configuration.joints):
            if index in envelope.commands:
                continue
            calibration=self.configuration.calibration_by_name[joint.name]
            # Recapture every uncommanded joint at its measured pose. This includes
            # the gripper: its measured angle can legitimately sit just outside the
            # narrower operational destination range while still remaining inside
            # the hard range. Reusing the configured home target here would create
            # an unintended gripper motion during an arm-only IK command.
            self.hold_reference[index]=float(self.feedback.positions_rad[index])
            feedforward=float(gravity[index]) if index < 6 else 0.0
            self.backend.send_impedance(index,float(self.hold_reference[index]),0.0,float(calibration["safe_float_kp"]),float(calibration["safe_float_kd"]),feedforward)
            self.active_command_modes[index]="IMPEDANCE"
            self.mit_moving_target[index]=float(self.hold_reference[index])
        for index, command in envelope.commands.items():
            values=command.values
            if command.mode == "IMPEDANCE":
                # Match the supplied Unity bridge: q_mt is a persistent moving
                # target. It advances toward the requested target at the local
                # send-loop rate and is not reset from measured q every tick.
                current=float(self.feedback.positions_rad[index])
                current_velocity=float(self.feedback.velocities_rad_s[index])
                if self.active_command_modes[index] != "IMPEDANCE":
                    self.mit_moving_target[index]=current
                target=float(values["position_rad"])
                target_rate=float(values.get("target_rate_limit_rad_s", self.configuration.joints[index].default_vlim))
                max_step=max(0.0005,target_rate*self.period)
                previous=float(self.mit_moving_target[index])
                self.mit_moving_target[index]=previous+float(np.clip(target-previous,-max_step,max_step))
                moving_target=float(self.mit_moving_target[index])
                target_velocity=float(values.get("velocity_rad_s",0.0))
                kp=float(values["kp"]); kd=float(values["kd"])
                position_error=moving_target-current; velocity_error=target_velocity-current_velocity
                raw_tracking_effort=kp*position_error+kd*velocity_error
                test_caps=self.configuration.model["joints"][index].get("provider_test_caps",{})
                effort_limit=max(0.0,float(test_caps.get("mit_tracking_effort_limit_nm",self.configuration.joints[index].configured_tmax_nm)))
                scale=1.0 if abs(raw_tracking_effort)<=effort_limit or abs(raw_tracking_effort)<1e-9 else effort_limit/abs(raw_tracking_effort)
                position_command=current+position_error*scale
                velocity_command=current_velocity+velocity_error*scale
                torque=float(values.get("feedforward_torque_nm",0.0))+float(gravity[index])
                self.backend.send_impedance(index,position_command,velocity_command,kp,kd,torque)
            elif command.mode == "POSITION_VELOCITY_LIMITED":
                if self._latched_endpoint_due_locked(index,command.mode,values):
                    self.backend.send_position_velocity(index,float(values["position_rad"]),float(values["velocity_limit_rad_s"]))
            elif command.mode == "VELOCITY": self.backend.send_velocity(index,float(values["velocity_rad_s"]))
            elif command.mode == "POSITION_EFFORT_LIMITED":
                if self._latched_endpoint_due_locked(index,command.mode,values):
                    self.backend.send_force_position(index,float(values["position_rad"]),float(values["velocity_limit_rad_s"]),float(values["torque_limit_ratio"]))
            self.active_command_modes[index]=command.mode
        self.last_applied_command_id=envelope.command_id

    def _latched_endpoint_due_locked(
        self,
        index: int,
        mode: str,
        values: dict[str, float],
    ) -> bool:
        signature=(
            mode,
            float(values["position_rad"]),
            float(values["velocity_limit_rad_s"]),
            float(values.get("torque_limit_ratio",-1.0)),
        )
        now=time.monotonic()
        due=(
            self.latched_endpoint_signatures[index] != signature
            or now-self.latched_endpoint_last_sent_monotonic[index] >= self.endpoint_keepalive_period_s
        )
        if due:
            self.latched_endpoint_signatures[index]=signature
            self.latched_endpoint_last_sent_monotonic[index]=now
            self.latched_endpoint_frames_sent += 1
        else:
            self.latched_endpoint_frames_suppressed += 1
        return due

    def _send_mode_hold_locked(self, index: int, mode: str, reference: float, gravity: np.ndarray) -> None:
        joint=self.configuration.joints[index]
        if mode == "IMPEDANCE":
            calibration=self.configuration.calibration_by_name[joint.name]
            feedforward=float(gravity[index]) if index < 6 else 0.0
            self.backend.send_impedance(
                index,
                reference,
                0.0,
                float(calibration["safe_float_kp"]),
                float(calibration["safe_float_kd"]),
                feedforward,
            )
        elif mode == "POSITION_VELOCITY_LIMITED":
            velocity_limit=min(0.12,max(0.02,float(joint.default_vlim)))
            self.backend.send_position_velocity(index,reference,velocity_limit)
        elif mode == "POSITION_EFFORT_LIMITED":
            velocity_limit=min(0.12,max(0.02,float(joint.default_vlim)))
            ratio=float(self.configuration.model["joints"][index]["default_test"].get("torque_limit_ratio",0.12))
            self.backend.send_force_position(index,reference,velocity_limit,ratio)
        elif mode == "VELOCITY":
            self.backend.send_velocity(index,0.0)
        else:
            raise RuntimeError(f"unsupported transition hold mode {mode}")

    def _apply_mode_transition_frame_locked(self, envelope: CommandEnvelope, gravity: np.ndarray) -> bool:
        """Change at most one motor mode per tick while every joint is held.

        Register mode changes can block while waiting for a serial confirmation.
        Sending fresh holds to all other joints before one change prevents six
        confirmation waits from accumulating into one unsupported control gap.
        Endpoint motion starts only after every requested mode is confirmed.
        """
        desired_modes=[
            envelope.commands[index].mode if index in envelope.commands else "IMPEDANCE"
            for index in range(7)
        ]
        signature=tuple(desired_modes)
        transitions=[
            index for index,desired in enumerate(desired_modes)
            if self.active_command_modes[index] is not None
            and self.active_command_modes[index] != desired
        ]
        if not transitions:
            self.mode_transition_signature=None
            self.mode_transition_hold_reference=None
            return False
        if self.mode_transition_signature != signature or self.mode_transition_hold_reference is None:
            self.mode_transition_signature=signature
            self.mode_transition_hold_reference=self.feedback.positions_rad.copy()
        reference=self.mode_transition_hold_reference
        selected=transitions[0]
        try:
            # Refresh every already-known mode before the potentially blocking
            # register operation. The selected joint is always changed last.
            for index in range(7):
                if index == selected:
                    continue
                current_mode=self.active_command_modes[index] or "IMPEDANCE"
                self._send_mode_hold_locked(index,current_mode,float(reference[index]),gravity)
            previous=self.active_command_modes[selected] or "UNKNOWN"
            desired=desired_modes[selected]
            self._send_mode_hold_locked(selected,desired,float(reference[selected]),gravity)
            self.active_command_modes[selected]=desired
            self.latched_endpoint_signatures[selected]=None
            self.latched_endpoint_last_sent_monotonic[selected]=0.0
            self.mode_transition_step_count+=1
            self.last_mode_transition_joint=selected
            self.last_mode_transition_from=previous
            self.last_mode_transition_to=desired
            return True
        except Exception:
            self.mode_transition_failure_count+=1
            raise


    def _apply_position_hold_locked(self) -> None:
        """Continuously hold all joints with POS_VEL; no MIT commands are sent."""
        assert self.feedback is not None
        for index in range(len(self.configuration.joints)):
            self.backend.send_position_velocity(index, float(self.hold_reference[index]), self.position_hold_velocity_limit)
            self.active_command_modes[index] = "POSITION_VELOCITY_LIMITED"
            self.mit_moving_target[index] = float(self.hold_reference[index])

    def _apply_gravity_float_locked(self, now: float) -> None:
        assert self.feedback is not None
        desired_gravity=self._gravity_with_payload_locked(self.feedback.positions_rad)
        ramp=max(float(self.configuration.model["control"]["gravity_float_entry_ramp_s"]),1e-3)
        alpha=float(np.clip((now-self.float_entry_started)/ramp,0.0,1.0))
        gravity=(1-alpha)*self.float_start_gravity+alpha*desired_gravity
        transition_indices=[
            index for index,mode in enumerate(self.active_command_modes)
            if mode not in (None,"IMPEDANCE")
        ]
        selected=transition_indices[0] if transition_indices else None
        # Refresh every non-selected joint before one potentially blocking mode
        # change. Motor-side joints retain a captured-position hold until their
        # individual transition into gravity-supported MIT is confirmed.
        order=[index for index in range(len(self.configuration.joints)) if index != selected]
        if selected is not None:
            order.append(selected)
        for index in order:
            joint=self.configuration.joints[index]
            current_mode=self.active_command_modes[index]
            if current_mode not in (None,"IMPEDANCE") and index != selected:
                self._send_mode_hold_locked(index,current_mode,float(self.hold_reference[index]),gravity)
                continue
            if current_mode not in (None,"IMPEDANCE") and index == selected:
                # Refresh the selected motor-side hold immediately before its
                # register-mode switch. This minimizes the unsupported interval
                # between the last POS_VEL/POS_TOR frame and the first MIT frame.
                self._send_mode_hold_locked(index,current_mode,float(self.hold_reference[index]),gravity)
            calibration=self.configuration.calibration_by_name[joint.name]
            kp=float(calibration["safe_float_kp"]); kd=float(calibration["safe_float_kd"])
            if index < 6:
                # Track the current measured arm position every cycle while using
                # the provided high spring stiffness. High kp supplies immediate
                # load support; updating the target prevents long-term rigid lock.
                # kd may remain comparatively low because it is velocity damping.
                self.hold_reference[index]=float(self.feedback.positions_rad[index])
                feedforward=float(gravity[index])
            else:
                # The gripper is not part of the rigid-body gravity model. Hold the
                # position captured on float entry with its configured MIT gains.
                feedforward=0.0
            self.backend.send_impedance(index,float(self.hold_reference[index]),0.0,kp,kd,feedforward)
            self.active_command_modes[index]="IMPEDANCE"
            self.mit_moving_target[index]=float(self.hold_reference[index])

    def snapshot_locked(self) -> dict[str, Any]:
        feedback=self.feedback
        if feedback is None:
            return {"state":self.state.value,"health":self.health,"ready":False}
        with self.ingress_lock:
            lease=None if self.lease is None else {
                "lease_id":self.lease.lease_id,
                "fencing_generation":self.lease.fencing_generation,
                "holder":self.lease.holder,
                "expires_in_ms":max(0,int((self.lease.expires_monotonic-time.monotonic())*1000)),
            }
            lease_diagnostics={
                "current":self._lease_status_ingress_locked(),
                "last_event":dict(self.last_lease_event),
                "last_drop_reason":self.last_lease_drop_reason,
            }
        return {
            "schema":"physical_agent.robot_arm_joint_state","schema_version":1,
            "observed_at_us":time.time_ns()//1000,"provider_state":self.state.value,"health":self.health,
            "positions_rad":feedback.positions_rad.tolist(),"velocities_rad_s":feedback.velocities_rad_s.tolist(),"torques_nm":feedback.torques_nm.tolist(),
            "temperatures_c":[None if not math.isfinite(float(v)) else float(v) for v in feedback.temperatures_c],
            "voltages_v":[None if not math.isfinite(float(v)) else float(v) for v in feedback.voltages_v],"motor_status":feedback.status_codes,
            "feedback_age_ms":max(0.0,(time.monotonic()-feedback.observed_monotonic)*1000.0),
            "last_applied_command_id":self.last_applied_command_id,"lease":lease,
            "active_command_modes":list(self.active_command_modes),
            "float_transition_pending_joint_indices":[
                index for index,mode in enumerate(self.active_command_modes)
                if mode not in (None,"IMPEDANCE")
            ],
            "command_ingress":{
                "semantics":"LATEST_VALID_ENVELOPE_REPLACES_PREVIOUS",
                "pending_command_id":None if self.pending is None else self.pending.command_id,
                "last_submitted_command_id":self.last_submitted_command_id,
                "last_replaced_command_id":self.last_replaced_command_id,
                "last_submitted_modes":list(self.last_submitted_modes),
                "submission_count":self.command_submission_count,
                "replacement_count":self.pending_replacement_count,
            },
            "mode_transition":{
                "active":self.mode_transition_signature is not None,
                "desired_modes":None if self.mode_transition_signature is None else list(self.mode_transition_signature),
                "step_count":self.mode_transition_step_count,
                "failure_count":self.mode_transition_failure_count,
                "last_joint_index":self.last_mode_transition_joint,
                "last_from":self.last_mode_transition_from,
                "last_to":self.last_mode_transition_to,
                "semantics":"ONE_MODE_CHANGE_PER_TICK_ALL_OTHER_JOINTS_REFRESHED_FIRST",
            },
            "latched_endpoint_output":{
                "keepalive_hz":1.0/self.endpoint_keepalive_period_s,
                "frames_sent":self.latched_endpoint_frames_sent,
                "frames_suppressed":self.latched_endpoint_frames_suppressed,
                "active_joint_indices":[
                    index for index,signature in enumerate(self.latched_endpoint_signatures)
                    if signature is not None
                ],
                "semantics":"CHANGED_ENDPOINT_IMMEDIATE_OTHERWISE_MOTOR_KEEPALIVE_RATE",
            },
            "loop":{
                "rate_hz":self.rate_hz,
                "loop_count":self.loop_count,
                "missed_deadlines":self.missed_deadlines,
                "missed_periods":self.missed_deadlines,
                "overrun_events":self.deadline_overrun_events,
                "last_lateness_ms":self.last_loop_lateness_ms,
                "max_lateness_ms":self.max_loop_lateness_ms,
                "last_tick_duration_ms":self.last_tick_duration_ms,
                "max_tick_duration_ms":self.max_tick_duration_ms,
                "control_fault_count":self.control_fault_count,
                "last_control_fault_at_us":self.last_control_fault_at_us,
            },
            "last_error":self.last_error,"last_float_reason":self.last_float_reason,"last_position_hold_reason":self.last_position_hold_reason,
            "lease_diagnostics":lease_diagnostics,
            "payload":self.dynamics.payload_snapshot(),
            "gravity_compensation":{
                "total_nm":self.last_gravity_compensation_nm.tolist(),
                "payload_nm":self.last_payload_gravity_nm.tolist(),
                "clamped_to_motor_tmax":list(self.gravity_compensation_clamped),
            },
            "hardware_io": self.backend.diagnostics() if hasattr(self.backend, "diagnostics") else {},
        }

    def _store_snapshot_cache(self, snapshot: dict[str, Any]) -> None:
        with self.snapshot_cache_lock:
            self.snapshot_cache=copy.deepcopy(snapshot)
            self.snapshot_cache_monotonic=time.monotonic()

    def _update_snapshot_cache_locked(self) -> dict[str, Any]:
        snapshot=self.snapshot_locked()
        self._store_snapshot_cache(snapshot)
        return snapshot

    def snapshot(self) -> dict[str, Any]:
        acquired=self.lock.acquire(timeout=0.02)
        if acquired:
            try:
                snapshot=self.snapshot_locked()
                self._store_snapshot_cache(snapshot)
                snapshot["snapshot_delivery"]={
                    "cached":False,
                    "cache_age_ms":0.0,
                }
                return snapshot
            finally:
                self.lock.release()
        with self.snapshot_cache_lock:
            snapshot=copy.deepcopy(self.snapshot_cache)
            cache_age_ms=max(0.0,(time.monotonic()-self.snapshot_cache_monotonic)*1000.0)
        snapshot["snapshot_delivery"]={
            "cached":True,
            "cache_age_ms":cache_age_ms,
            "reason":"control lock is occupied by motor I/O",
        }
        return snapshot
