"""Hardware abstraction and deterministic simulation for reBot Arm DM."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
import importlib
import math
import threading
import time

import numpy as np

from .models import ArmConfiguration


@dataclass
class JointFeedback:
    positions_rad: np.ndarray
    velocities_rad_s: np.ndarray
    torques_nm: np.ndarray
    temperatures_c: np.ndarray
    voltages_v: np.ndarray
    status_codes: list[str]
    observed_at_us: int
    observed_monotonic: float
    timestamp_uncertainty_us: int
    per_joint_observed_at_us: list[int]
    feedback_generations: list[int]
    freshness_verified: bool
    freshness_source: str
    acquisition_duration_ms: float


class HardwareBackend(Protocol):
    def configure_inactive_joints(self, indices: set[int] | frozenset[int]) -> None: ...
    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    def enable(self) -> None: ...
    def disable(self) -> None: ...
    def read(self) -> JointFeedback: ...
    def send_impedance(self, index: int, position: float, velocity: float, kp: float, kd: float, torque: float) -> None: ...
    def send_position_velocity(self, index: int, position: float, velocity_limit: float) -> None: ...
    def send_velocity(self, index: int, velocity: float) -> None: ...
    def send_force_position(self, index: int, position: float, velocity_limit: float, torque_ratio: float) -> None: ...


class SimulationBackend:
    """Seven-joint low-order simulation suitable for UI and safety tests."""

    def __init__(self, configuration: ArmConfiguration, gravity_function=None):
        self.configuration = configuration
        self.gravity_function = gravity_function
        self.lock = threading.RLock()
        self.connected = False
        self.enabled = False
        self.position = configuration.home_positions.copy()
        self.velocity = np.zeros(7)
        self.torque = np.zeros(7)
        self.temperature = np.full(7, 28.0)
        self.voltage = np.full(7, 24.0)
        self.last_time = time.monotonic()
        self.commands = [dict(mode="IDLE", position=float(self.position[i]), velocity=0.0, kp=0.0, kd=0.0, torque=0.0, vlim=0.2, ratio=0.0) for i in range(7)]
        self.read_cycle_count = 0
        self.command_frame_count = 0
        self.started_monotonic = time.monotonic()
        self.inactive_joint_indices: frozenset[int] = frozenset()

    def configure_inactive_joints(self, indices: set[int] | frozenset[int]) -> None:
        if self.connected:
            raise RuntimeError("inactive joints must be configured before backend connection")
        normalized = frozenset(int(index) for index in indices)
        if any(index < 0 or index >= len(self.configuration.joints) for index in normalized):
            raise ValueError("inactive joint index is outside the configured model")
        self.inactive_joint_indices = normalized

    def _assert_active(self, index: int) -> None:
        if index in self.inactive_joint_indices:
            raise RuntimeError(f"joint {index + 1} is inactive in the installed assembly")

    def connect(self) -> None:
        with self.lock:
            self.connected = True; self.last_time = time.monotonic()

    def disconnect(self) -> None:
        with self.lock:
            self.connected = False; self.enabled = False

    def enable(self) -> None:
        if not self.connected: raise RuntimeError("simulation backend is disconnected")
        self.enabled = True

    def disable(self) -> None:
        self.enabled = False

    def _step(self) -> None:
        now=time.monotonic(); dt=min(max(now-self.last_time, 0.0), 0.05); self.last_time=now
        if not self.enabled or dt <= 0: return
        physical_gravity = np.zeros(7) if self.gravity_function is None else np.asarray(self.gravity_function(self.position), dtype=float)
        for i, command in enumerate(self.commands):
            mode=command["mode"]
            friction = 0.05 * math.tanh(self.velocity[i] / 0.03) + 0.03 * self.velocity[i]
            if mode == "IMPEDANCE":
                drive = command["kp"]*(command["position"]-self.position[i]) + command["kd"]*(command["velocity"]-self.velocity[i]) + command["torque"]
                acceleration = (drive - physical_gravity[i] - friction) / 0.12
                acceleration = float(np.clip(acceleration, -6.0, 6.0))
            elif mode in {"POSITION_VELOCITY_LIMITED", "POSITION_EFFORT_LIMITED"}:
                error=command["position"]-self.position[i]
                desired=float(np.clip(error*4.0, -command["vlim"], command["vlim"]))
                acceleration=float(np.clip((desired-self.velocity[i])*12.0, -5.0, 5.0))
                if mode == "POSITION_EFFORT_LIMITED": acceleration *= max(0.05, command["ratio"])
            elif mode == "VELOCITY":
                acceleration=float(np.clip((command["velocity"]-self.velocity[i])*10.0, -5.0, 5.0))
            else:
                acceleration=-self.velocity[i]*8.0
            self.velocity[i]+=acceleration*dt
            self.position[i]+=self.velocity[i]*dt
            hard=self.configuration.hard_limits[i]
            if self.position[i] < hard[0] or self.position[i] > hard[1]:
                self.position[i]=float(np.clip(self.position[i], hard[0], hard[1])); self.velocity[i]=0.0
            self.torque[i]=physical_gravity[i] + 0.12*acceleration + friction
            self.temperature[i]+=abs(self.torque[i])*dt*0.002

    def read(self) -> JointFeedback:
        with self.lock:
            if not self.connected: raise RuntimeError("simulation backend is disconnected")
            acquisition_started = time.monotonic()
            self.read_cycle_count += 1
            self._step()
            observed_monotonic = time.monotonic()
            observed_at_us = time.time_ns() // 1000
            return JointFeedback(
                positions_rad=self.position.copy(),
                velocities_rad_s=self.velocity.copy(),
                torques_nm=self.torque.copy(),
                temperatures_c=self.temperature.copy(),
                voltages_v=self.voltage.copy(),
                status_codes=[
                    "INACTIVE_NOT_INSTALLED"
                    if index in self.inactive_joint_indices
                    else "OK"
                    for index in range(7)
                ],
                observed_at_us=observed_at_us,
                observed_monotonic=observed_monotonic,
                timestamp_uncertainty_us=0,
                per_joint_observed_at_us=[observed_at_us] * 7,
                feedback_generations=[self.read_cycle_count] * 7,
                freshness_verified=True,
                freshness_source="SIMULATION_STEP_COMPLETION",
                acquisition_duration_ms=(observed_monotonic - acquisition_started) * 1000.0,
            )

    def send_impedance(self, index, position, velocity, kp, kd, torque):
        self._assert_active(index)
        with self.lock: self.command_frame_count += 1; self.commands[index].update(mode="IMPEDANCE", position=position, velocity=velocity, kp=kp, kd=kd, torque=torque)
    def send_position_velocity(self, index, position, velocity_limit):
        self._assert_active(index)
        with self.lock: self.command_frame_count += 1; self.commands[index].update(mode="POSITION_VELOCITY_LIMITED", position=position, vlim=velocity_limit)
    def send_velocity(self, index, velocity):
        self._assert_active(index)
        with self.lock: self.command_frame_count += 1; self.commands[index].update(mode="VELOCITY", velocity=velocity)
    def send_force_position(self, index, position, velocity_limit, torque_ratio):
        self._assert_active(index)
        with self.lock: self.command_frame_count += 1; self.commands[index].update(mode="POSITION_EFFORT_LIMITED", position=position, vlim=velocity_limit, ratio=torque_ratio)

    def diagnostics(self) -> dict[str, Any]:
        elapsed = max(time.monotonic() - self.started_monotonic, 1e-6)
        return {
            "backend": "SIMULATION",
            "read_cycles": self.read_cycle_count,
            "command_frames": self.command_frame_count,
            "read_cycles_per_s": self.read_cycle_count / elapsed,
            "command_frames_per_s": self.command_frame_count / elapsed,
            "inactive_joint_names": [
                self.configuration.joints[index].name
                for index in sorted(self.inactive_joint_indices)
            ],
        }


class MotorBridgeBackend:
    """Direct adapter for the reviewed MotorBridge 0.5.1 freshness API."""

    MODE_NAMES = {
        "IMPEDANCE": "MIT",
        "POSITION_VELOCITY_LIMITED": "POS_VEL",
        "VELOCITY": "VEL",
        "POSITION_EFFORT_LIMITED": "FORCE_POS",
    }

    def __init__(self, configuration: ArmConfiguration, port: str, baudrate: int = 921600):
        self.configuration=configuration; self.port=port; self.baudrate=baudrate
        self.controller=None; self.motors=[]; self.mode_enum=None; self.active_modes=[None]*7; self.enabled=False
        self.inactive_joint_indices: frozenset[int] = frozenset()
        self.started_monotonic=time.monotonic(); self.read_cycle_count=0; self.feedback_request_count=0
        self.feedback_poll_count=0; self.command_frame_attempt_count=0; self.command_frame_count=0
        self.mode_switch_count=0; self.mode_switch_attempt_count=0; self.mode_switch_failure_count=0
        self.io_error_count=0; self.last_io_error=None; self.last_mode_switch_error=None
        self.feedback_stale_rejection_count=0; self.last_missing_feedback_joint_names=[]
        self.last_feedback_acquisition_duration_ms=0.0; self.last_feedback_timestamp_uncertainty_us=0
        control=configuration.model.get("control",{})
        self.transient_retry_limit=int(control.get("transient_serial_retry_count",0))
        self.transient_retry_delay_s=float(control.get("transient_serial_retry_delay_ms",0.0))/1000.0
        self.mit_mode_confirmation_timeout_ms=int(
            control.get("mit_mode_confirmation_timeout_ms",250)
        )
        self.feedback_cycle_timeout_s=float(
            control.get("feedback_cycle_timeout_ms",40.0)
        )/1000.0
        self.feedback_rerequest_interval_s=float(
            control.get("feedback_rerequest_interval_ms",4.0)
        )/1000.0
        self.transient_retry_attempt_count=0; self.transient_retry_recovery_count=0
        self.transient_retry_failure_count=0; self.last_transient_retry_error=None

    def configure_inactive_joints(self, indices: set[int] | frozenset[int]) -> None:
        if self.controller is not None:
            raise RuntimeError("inactive joints must be configured before backend connection")
        normalized = frozenset(int(index) for index in indices)
        if any(index < 0 or index >= len(self.configuration.joints) for index in normalized):
            raise ValueError("inactive joint index is outside the configured model")
        self.inactive_joint_indices = normalized

    def connect(self) -> None:
        try:
            module=importlib.import_module("motorbridge")
        except ImportError as error:
            raise RuntimeError("MotorBridge is not installed. Run setup.ps1 -WithMotorBridge.") from error
        Controller=getattr(module,"Controller",None); self.mode_enum=getattr(module,"Mode",None)
        if Controller is None or self.mode_enum is None:
            raise RuntimeError("MotorBridge must export Controller and Mode")
        if not hasattr(Controller,"from_dm_serial"):
            raise RuntimeError("Controller.from_dm_serial is unavailable; MotorBridge 0.5.1 or later is required")
        Motor=getattr(module,"Motor",None)
        if Motor is not None and not callable(getattr(Motor,"get_state_sample",None)):
            raise RuntimeError(
                "MotorBridge does not expose verified feedback generations and receive ages; "
                "run providers/rebot_arm_dm/scripts/setup.ps1 -WithMotorBridge to install "
                "the reviewed freshness build"
            )
        self.controller=Controller.from_dm_serial(self.port,self.baudrate)
        self.started_monotonic=time.monotonic()
        self.motors=[None]*len(self.configuration.joints); self.active_modes=[None]*7
        for joint in self.configuration.joints:
            if joint.index in self.inactive_joint_indices:
                continue
            self.motors[joint.index]=self.controller.add_damiao_motor(
                joint.motor_id,
                joint.feedback_id,
                joint.motor_model,
            )
        active_motors=[motor for motor in self.motors if motor is not None]
        if not active_motors:
            self.disconnect()
            raise RuntimeError("installed assembly contains no active motors")
        if not all(callable(getattr(motor,"get_state_sample",None)) for motor in active_motors):
            self.disconnect()
            raise RuntimeError(
                "MotorBridge does not expose verified feedback generations and receive ages; "
                "run providers/rebot_arm_dm/scripts/setup.ps1 -WithMotorBridge to install "
                "the reviewed freshness build"
            )

    def disconnect(self) -> None:
        if self.controller is not None:
            try: self.controller.disable_all()
            except Exception: pass
            try: self.controller.shutdown()
            except Exception: pass
            try: self.controller.close()
            except Exception: pass
        self.controller=None; self.motors=[]; self.enabled=False

    def enable(self) -> None:
        if self.controller is None: raise RuntimeError("MotorBridge is disconnected")
        self.controller.enable_all(); self.enabled=True

    def disable(self) -> None:
        if self.controller is not None:
            try: self.controller.disable_all()
            except Exception: pass
        self.enabled=False

    def _joint_from_motor(self,index:int,position:float,velocity:float,torque:float) -> tuple[float,float,float]:
        calibration=self.configuration.calibration_by_name[self.configuration.joints[index].name]
        sign=float(calibration.get("direction_sign",1.0)); offset=float(calibration.get("zero_offset_rad",0.0)); scale=max(abs(float(calibration.get("torque_scale",1.0))),1e-9)
        return sign*position+offset,sign*velocity,sign*torque*scale

    def _motor_position(self,index:int,joint_position:float) -> float:
        calibration=self.configuration.calibration_by_name[self.configuration.joints[index].name]
        sign=float(calibration.get("direction_sign",1.0)); offset=float(calibration.get("zero_offset_rad",0.0))
        return (joint_position-offset)/sign

    def _motor_velocity(self,index:int,joint_velocity:float) -> float:
        sign=float(self.configuration.calibration_by_name[self.configuration.joints[index].name].get("direction_sign",1.0))
        return joint_velocity/sign

    def _motor_torque(self,index:int,joint_torque:float) -> float:
        calibration=self.configuration.calibration_by_name[self.configuration.joints[index].name]
        sign=float(calibration.get("direction_sign",1.0)); scale=max(abs(float(calibration.get("torque_scale",1.0))),1e-9)
        return joint_torque/(sign*scale)

    @staticmethod
    def _is_transient_serial_error(exc: Exception) -> bool:
        message=str(exc).lower()
        return (
            "semaphore timeout period has expired" in message
            or "os error 121" in message
            or "device does not recognize the command" in message
            or "os error 22" in message
            or ("register 10" in message and "not received" in message)
        )

    def _with_transient_retry(self, operation) -> Any:
        attempts=0
        while True:
            try:
                result=operation()
                if attempts:
                    self.transient_retry_recovery_count += 1
                    self.last_transient_retry_error=None
                return result
            except Exception as exc:
                if not self._is_transient_serial_error(exc) or attempts>=self.transient_retry_limit:
                    if attempts:
                        self.transient_retry_failure_count += 1
                    raise
                attempts += 1
                self.transient_retry_attempt_count += 1
                self.last_transient_retry_error=str(exc)
                if self.transient_retry_delay_s>0.0:
                    time.sleep(self.transient_retry_delay_s)

    def read(self) -> JointFeedback:
        if self.controller is None: raise RuntimeError("MotorBridge is disconnected")
        acquisition_started_monotonic=time.monotonic()
        active_indices=[
            index for index,motor in enumerate(self.motors)
            if motor is not None
        ]
        baselines={}
        try:
            for index in active_indices:
                motor=self.motors[index]
                sample=motor.get_state_sample()
                baselines[index]=0 if sample is None else int(sample[1])
        except Exception as exc:
            self.io_error_count += 1; self.last_io_error = str(exc); raise

        pending=set(active_indices)
        samples:dict[int,tuple[Any,int,int,int,float]]={}
        deadline=acquisition_started_monotonic+self.feedback_cycle_timeout_s
        next_request_at=acquisition_started_monotonic
        while pending:
            now=time.monotonic()
            if now>=next_request_at:
                try:
                    for index in sorted(pending):
                        self.feedback_request_count += 1
                        self._with_transient_retry(self.motors[index].request_feedback)
                    next_request_at=now+self.feedback_rerequest_interval_s
                except Exception as exc:
                    self.io_error_count += 1; self.last_io_error = str(exc); raise
            try:
                self.feedback_poll_count += 1
                self._with_transient_retry(self.controller.poll_feedback_once)
                for index in list(pending):
                    call_started_us=time.time_ns()//1000
                    sample=self.motors[index].get_state_sample()
                    call_completed_us=time.time_ns()//1000
                    if sample is None or int(sample[1])<=baselines[index]:
                        continue
                    state,generation,age_us=sample
                    receive_upper_us=call_completed_us-int(age_us)
                    receive_lower_us=call_started_us-int(age_us)
                    observed_at_us=(receive_lower_us+receive_upper_us)//2
                    uncertainty_us=max(1,(receive_upper_us-receive_lower_us+1)//2)
                    observed_monotonic=time.monotonic()-max(0,int(age_us))/1_000_000.0
                    samples[index]=(state,int(generation),observed_at_us,uncertainty_us,observed_monotonic)
                    pending.remove(index)
            except Exception as exc:
                self.io_error_count += 1; self.last_io_error = str(exc); raise
            if not pending:
                break
            if time.monotonic()>=deadline:
                self.feedback_stale_rejection_count += 1
                self.last_missing_feedback_joint_names=[
                    self.configuration.joints[index].name for index in sorted(pending)
                ]
                missing_text=", ".join(self.last_missing_feedback_joint_names)
                raise RuntimeError(
                    "fresh feedback generation did not advance within "
                    f"{self.feedback_cycle_timeout_s * 1000.0:.1f} ms for: "
                    f"{missing_text}"
                )
            time.sleep(min(0.001,max(0.0,deadline-time.monotonic())))

        joint_count=len(self.configuration.joints)
        positions=self.configuration.home_positions.astype(float).tolist()
        velocities=[0.0]*joint_count
        torques=[0.0]*joint_count
        status=["INACTIVE_NOT_INSTALLED"]*joint_count
        per_joint_observed_at_us=[0]*joint_count
        generations=[0]*joint_count
        interval_start_us=[]; interval_end_us=[]; observed_monotonic_values=[]
        for index in active_indices:
            state,generation,observed_at_us,uncertainty_us,observed_monotonic=samples[index]
            q,qd,tau=self._joint_from_motor(index,float(state.pos),float(state.vel),float(state.torq))
            positions[index]=q; velocities[index]=qd; torques[index]=tau; status[index]=str(state.status_code)
            per_joint_observed_at_us[index]=observed_at_us; generations[index]=generation
            interval_start_us.append(observed_at_us-uncertainty_us)
            interval_end_us.append(observed_at_us+uncertainty_us)
            observed_monotonic_values.append(observed_monotonic)
        values=np.asarray(positions,dtype=float)
        if not np.all(np.isfinite(values)): raise RuntimeError("MotorBridge returned non-finite positions")
        batch_start_us=min(interval_start_us); batch_end_us=max(interval_end_us)
        observed_at_us=(batch_start_us+batch_end_us)//2
        timestamp_uncertainty_us=max(1,(batch_end_us-batch_start_us+1)//2)
        observed_monotonic=(min(observed_monotonic_values)+max(observed_monotonic_values))/2.0
        acquisition_duration_ms=(time.monotonic()-acquisition_started_monotonic)*1000.0
        self.read_cycle_count += 1
        self.last_missing_feedback_joint_names=[]
        self.last_feedback_acquisition_duration_ms=acquisition_duration_ms
        self.last_feedback_timestamp_uncertainty_us=timestamp_uncertainty_us
        return JointFeedback(
            positions_rad=values,
            velocities_rad_s=np.asarray(velocities),
            torques_nm=np.asarray(torques),
            temperatures_c=np.full(7,np.nan),
            voltages_v=np.full(7,np.nan),
            status_codes=status,
            observed_at_us=observed_at_us,
            observed_monotonic=observed_monotonic,
            timestamp_uncertainty_us=timestamp_uncertainty_us,
            per_joint_observed_at_us=per_joint_observed_at_us,
            feedback_generations=generations,
            freshness_verified=True,
            freshness_source="MOTORBRIDGE_GENERATION_ADVANCED_WITH_RECEIVE_AGE",
            acquisition_duration_ms=acquisition_duration_ms,
        )

    def _motor(self,index:int):
        if index<0 or index>=len(self.motors): raise IndexError(index)
        motor=self.motors[index]
        if motor is None:
            raise RuntimeError(f"joint {index + 1} is inactive in the installed assembly")
        return motor

    def _ensure_mode(self,index:int,canonical_mode:str) -> bool:
        mode_name=self.MODE_NAMES[canonical_mode]
        if self.active_modes[index]==mode_name: return False
        mode=getattr(self.mode_enum,mode_name,None)
        if mode is None: raise RuntimeError(f"MotorBridge Mode.{mode_name} is unavailable")
        self.mode_switch_attempt_count += 1
        try:
            self._with_transient_retry(
                lambda: self._motor(index).ensure_mode(
                    mode,
                    self.mit_mode_confirmation_timeout_ms,
                )
            )
        except Exception as exc:
            # A missing confirmation means the motor's actual mode is unknown.
            # Never retain the old cache value and then send a frame under that
            # assumption during gravity-float recovery.
            self.active_modes[index]=None
            self.mode_switch_failure_count += 1
            self.last_mode_switch_error=f"joint {index + 1} -> {mode_name}: {exc}"
            raise RuntimeError(f"mode switch {self.last_mode_switch_error}") from exc
        self.active_modes[index]=mode_name
        self.mode_switch_count += 1
        self.last_mode_switch_error=None
        return True

    def _send_frame(self, operation) -> None:
        self.command_frame_attempt_count += 1
        try:
            self._with_transient_retry(operation)
        except Exception as exc:
            self.io_error_count += 1
            self.last_io_error = str(exc)
            raise
        self.command_frame_count += 1

    def send_impedance(self,index,position,velocity,kp,kd,torque):
        operation=lambda: self._motor(index).send_mit(self._motor_position(index,position),self._motor_velocity(index,velocity),kp,kd,self._motor_torque(index,torque))
        motor=self._motor(index)
        mode_name=self.MODE_NAMES["IMPEDANCE"]
        supports_early_bridge=(
            self.active_modes[index] in {
                self.MODE_NAMES["POSITION_VELOCITY_LIMITED"],
                self.MODE_NAMES["POSITION_EFFORT_LIMITED"],
            }
            and hasattr(motor,"write_register_u32")
            and hasattr(motor,"get_register_u32")
        )
        if supports_early_bridge:
            self.mode_switch_attempt_count += 1
            try:
                # Register 10 is CTRL_MODE. The serial/CAN ordering places the
                # supporting MIT frame directly after the mode write, before
                # waiting for the read-back confirmation that previously left
                # the motor with cleared command values.
                self._with_transient_retry(
                    lambda: motor.write_register_u32(10,int(self.mode_enum.MIT))
                )
                self._send_frame(operation)
                observed=int(
                    self._with_transient_retry(
                        lambda: motor.get_register_u32(
                            10,
                            self.mit_mode_confirmation_timeout_ms,
                        )
                    )
                )
                if observed!=int(self.mode_enum.MIT):
                    raise RuntimeError(
                        f"register 10 confirmed unexpected mode {observed}"
                    )
            except Exception as exc:
                self.active_modes[index]=None
                self.mode_switch_failure_count += 1
                self.last_mode_switch_error=f"joint {index + 1} -> {mode_name}: {exc}"
                raise RuntimeError(f"mode switch {self.last_mode_switch_error}") from exc
            self.active_modes[index]=mode_name
            self.mode_switch_count += 1
            self.last_mode_switch_error=None
            changed=True
        else:
            changed=self._ensure_mode(index,"IMPEDANCE")
        self._send_frame(operation)
        # Older MotorBridge-compatible objects without the public register API
        # still receive a duplicated first post-confirmation frame.
        if changed and not supports_early_bridge:
            self._send_frame(operation)

    def send_position_velocity(self,index,position,velocity_limit):
        self._ensure_mode(index,"POSITION_VELOCITY_LIMITED")
        self._send_frame(lambda: self._motor(index).send_pos_vel(self._motor_position(index,position),abs(float(velocity_limit))))

    def send_velocity(self,index,velocity):
        self._ensure_mode(index,"VELOCITY")
        self._send_frame(lambda: self._motor(index).send_vel(self._motor_velocity(index,velocity)))

    def send_force_position(self,index,position,velocity_limit,torque_ratio):
        self._ensure_mode(index,"POSITION_EFFORT_LIMITED")
        self._send_frame(lambda: self._motor(index).send_force_pos(self._motor_position(index,position),abs(float(velocity_limit)),torque_ratio))

    def diagnostics(self) -> dict[str, Any]:
        elapsed = max(time.monotonic() - self.started_monotonic, 1e-6)
        return {
            "backend": "MOTORBRIDGE_DM_SERIAL",
            "port": self.port,
            "baudrate": self.baudrate,
            "active_joint_names": [
                self.configuration.joints[index].name
                for index,motor in enumerate(self.motors)
                if motor is not None
            ],
            "inactive_joint_names": [
                self.configuration.joints[index].name
                for index in sorted(self.inactive_joint_indices)
            ],
            "read_cycles": self.read_cycle_count,
            "feedback_requests": self.feedback_request_count,
            "feedback_polls": self.feedback_poll_count,
            "feedback_cycle_timeout_ms": self.feedback_cycle_timeout_s * 1000.0,
            "feedback_rerequest_interval_ms": self.feedback_rerequest_interval_s * 1000.0,
            "feedback_stale_rejections": self.feedback_stale_rejection_count,
            "last_missing_feedback_joint_names": list(self.last_missing_feedback_joint_names),
            "last_feedback_acquisition_duration_ms": self.last_feedback_acquisition_duration_ms,
            "last_feedback_timestamp_uncertainty_us": self.last_feedback_timestamp_uncertainty_us,
            "freshness_semantics": "EVERY_JOINT_GENERATION_MUST_ADVANCE_AFTER_BATCH_REQUEST",
            "command_frame_attempts": self.command_frame_attempt_count,
            "command_frames": self.command_frame_count,
            "mode_switches": self.mode_switch_count,
            "mode_switch_attempts": self.mode_switch_attempt_count,
            "mode_switch_failures": self.mode_switch_failure_count,
            "last_mode_switch_error": self.last_mode_switch_error,
            "transient_retry_limit": self.transient_retry_limit,
            "mit_mode_confirmation_timeout_ms": self.mit_mode_confirmation_timeout_ms,
            "transient_retry_attempts": self.transient_retry_attempt_count,
            "transient_retry_recoveries": self.transient_retry_recovery_count,
            "transient_retry_failures": self.transient_retry_failure_count,
            "last_transient_retry_error": self.last_transient_retry_error,
            "io_errors": self.io_error_count,
            "last_io_error": self.last_io_error,
            "read_cycles_per_s": self.read_cycle_count / elapsed,
            "command_frames_per_s": self.command_frame_count / elapsed,
        }
