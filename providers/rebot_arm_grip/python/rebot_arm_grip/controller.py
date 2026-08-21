from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import math
import os
import threading
import time

from .authorization import AuthorizationError, canonical_sha256, verify_assertion


POS_TOR = "POSITION_EFFORT_LIMITED"
MIT = "IMPEDANCE"


@dataclass
class GripTarget:
    position_rad: float
    velocity_limit_rad_s: float
    torque_limit_nm: float
    intent: str


@dataclass
class FloatTransition:
    started_monotonic: float
    duration_s: float
    start_position_rad: float
    end_position_rad: float
    kp: float
    kd: float


@dataclass
class MitPositionTransition:
    started_monotonic: float
    requested_duration_s: float
    resolved_duration_s: float
    start_position_rad: float
    end_position_rad: float
    target_rate_limit_rad_s: float
    kp: float
    kd: float


@dataclass
class MitPositionHold:
    position_rad: float
    target_rate_limit_rad_s: float
    kp: float
    kd: float


class ThermalGateError(PermissionError):
    def __init__(self, message: str, retry_after_s: float, temperatures: list[Any]):
        super().__init__(message)
        self.retry_after_s = float(retry_after_s)
        self.temperatures = list(temperatures)


class GripController:
    """Own the gripper command stream and runtime carrying state."""

    def __init__(
        self,
        config: dict[str, Any],
        basic: Any,
        contact_http: Any,
        *,
        provider_instance_id: str,
        provider_boot_id: str,
    ):
        self.config = config
        self.basic = basic
        self.contact_http = contact_http
        self.provider_id = str(config["provider_id"])
        self.provider_instance_id = provider_instance_id
        self.provider_boot_id = provider_boot_id
        self.lock = threading.RLock()
        self.operation_lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.initialized = False
        self.residency = "WARM"
        self.health = "STARTING"
        self.state = "MIT_FLOAT"
        self.last_error: str | None = None
        self.last_command_monotonic: float | None = None
        self.last_feedback: dict[str, Any] = {}
        self.target: GripTarget | None = None
        self.float_transition: FloatTransition | None = None
        self.mit_position_transition: MitPositionTransition | None = None
        self.mit_position_hold: MitPositionHold | None = None
        self.contact_samples = 0
        self.contact_inferred = False
        self.carry: dict[str, Any] | None = None
        self.used_assertion_ids: dict[str, int] = {}
        self.assembly_fingerprint = ""
        self.mounted_effector_revision = ""
        self.gripper_resource_id = ""
        self.gripper_joint_index = -1
        self.active_joint_indices: list[int] = []
        self.profile: dict[str, Any] = {}

    @property
    def period_s(self) -> float:
        return 1.0 / float(self.config["control"]["rate_hz"])

    def start(self) -> None:
        with self.operation_lock:
            with self.lock:
                if (
                    self.thread
                    and self.thread.is_alive()
                    and not self.stop_event.is_set()
                    and self.residency == "HOT"
                ):
                    return
            self._initialize()
            with self.lock:
                if self.thread and self.thread.is_alive():
                    self.residency = "HOT"
                    return
                self.stop_event.clear()
                self.residency = "HOT"
                self.health = "HEALTHY"
                self.thread = threading.Thread(
                    target=self._loop, name="grip-control-50hz", daemon=True
                )
                self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        thread = self.thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        with self.lock:
            carrying = self.carry is not None
        if not carrying:
            try:
                self._finish_float("Grip Provider stopped")
            except Exception as exc:
                with self.lock:
                    self.last_error = str(exc)
                    self.health = "DEGRADED"

    def enter_warm(self) -> dict[str, Any]:
        with self.operation_lock:
            with self.lock:
                if self.carry is not None:
                    raise PermissionError("cannot enter WARM while carrying")
            self._finish_float("Grip Provider entered WARM")
            with self.lock:
                self.residency = "WARM"
            return {"status": "warm", "state": self.state}

    def _initialize(self) -> None:
        model = self.basic.model()
        assembly = self.basic.assembly()
        joints = model.get("joints")
        if not isinstance(joints, list) or not joints:
            raise RuntimeError("Basic model does not expose joints")
        groups = assembly.get("resource_groups")
        if not isinstance(groups, list):
            raise RuntimeError("Basic assembly does not expose resource groups")
        group = next((item for item in groups if item.get("group_id") == "gripper"), None)
        if not isinstance(group, dict):
            raise RuntimeError("active assembly has no gripper actuator group")
        names = group.get("joint_names")
        if not isinstance(names, list) or len(names) != 1:
            raise RuntimeError("gripper actuator group must contain exactly one joint")
        joint_index = next(
            (index for index, joint in enumerate(joints) if joint.get("name") == names[0]),
            None,
        )
        if joint_index is None:
            raise RuntimeError("gripper actuator group references an unknown joint")
        mounted = assembly.get("mounted_effector")
        if not isinstance(mounted, dict):
            raise RuntimeError("Basic assembly has no mounted-effector profile")
        extension = self.config.get("effector_control_profile")
        if not isinstance(extension, dict):
            raise RuntimeError("Grip Provider has no gripper effector-control profile")
        joint_control = extension.get("joint_control")
        if not isinstance(joint_control, dict):
            raise RuntimeError("Grip Provider has no joint-control profile")
        operational = joint_control.get("operational_position_range_rad")
        if not isinstance(operational, list) or len(operational) != 2:
            raise RuntimeError("Grip Provider operational range is invalid")
        low, high = (float(operational[0]), float(operational[1]))
        fully_open = float(joint_control["fully_open_position_rad"])
        functional_open = float(joint_control["open_position_rad"])
        default_grip = float(joint_control["default_grip_position_rad"])
        if not all(
            math.isfinite(value)
            for value in (low, high, fully_open, functional_open, default_grip)
        ) or not (low <= fully_open <= functional_open < default_grip <= high):
            raise RuntimeError(
                "Grip Provider requires increasing-position closure from fully open through functional open to the default grip target"
            )
        if joint_control.get("closing_direction") != "INCREASING_POSITION_RAD":
            raise RuntimeError(
                "Grip Provider position convention does not match the physical gripper"
            )
        compatibility = extension.get("compatible_mounted_effector")
        if not isinstance(compatibility, dict) or any(
            compatibility.get(name) != mounted.get(name)
            for name in ("profile_id", "profile_revision")
        ):
            raise RuntimeError("grip-control profile does not match the mounted effector")
        for transition_name in ("mit_position_transition", "mit_float_transition"):
            profile_rate = float(extension[transition_name]["interpolation_rate_hz"])
            if not math.isclose(
                profile_rate,
                self.period_s**-1,
                rel_tol=0.0,
                abs_tol=1e-9,
            ):
                raise RuntimeError(
                    f"Grip Provider rate does not match {transition_name}"
                )
        position_transition = extension["mit_position_transition"]
        if not (
            0.02
            <= float(position_transition["default_duration_s"])
            <= float(position_transition["maximum_duration_s"])
        ):
            raise RuntimeError("Grip Provider MIT position duration bounds are invalid")
        required_open_duration = max(
            abs(functional_open - low), abs(high - functional_open)
        ) / float(joint_control["maximum_velocity_rad_s"])
        if required_open_duration > float(position_transition["maximum_duration_s"]):
            raise RuntimeError(
                "Grip Provider cannot reach functional open within its velocity and duration limits"
            )
        detection = extension.get("contact_detection")
        if not isinstance(detection, dict) or detection.get("decision_basis") != (
            "ABSOLUTE_MEASURED_MOTOR_TORQUE_ONLY"
        ):
            raise RuntimeError("Grip Provider contact-decision basis is unsupported")
        minimum_contact_torque = float(
            detection["minimum_absolute_measured_torque_nm"]
        )
        stable_contact_samples = int(detection["stable_samples_at_50hz"])
        if (
            not math.isfinite(minimum_contact_torque)
            or minimum_contact_torque <= 0.0
            or stable_contact_samples < 1
        ):
            raise RuntimeError("Grip Provider torque-only contact thresholds are invalid")
        profile_gate = float(extension["thermal_policy"]["new_grip_gate_c"])
        if not math.isclose(
            profile_gate,
            float(self.config["thermal"]["new_grip_gate_c"]),
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            raise RuntimeError("Grip Provider thermal gate does not match the effector profile")
        active_indices = [
            index
            for index, joint in enumerate(joints)
            if str(joint.get("name")) not in set(mounted.get("inactive_joint_names", []))
        ]
        self.basic.bind_resource(str(group["resource_id"]), int(joint_index))
        with self.lock:
            self.gripper_resource_id = str(group["resource_id"])
            self.gripper_joint_index = int(joint_index)
            self.active_joint_indices = active_indices
            self.assembly_fingerprint = str(assembly["assembly_fingerprint"])
            self.mounted_effector_revision = str(mounted["profile_revision"])
            self.profile = extension
            self.initialized = True

    def _authorize(self, command: dict[str, Any], token: str) -> dict[str, Any]:
        skill_id = str(command.get("skill_id") or "").strip()
        env_name = self.config["authorization"]["skill_secret_envs"].get(skill_id)
        if not env_name:
            raise AuthorizationError("Grip Skill is not allowlisted")
        secret = os.getenv(str(env_name), "")
        payload = verify_assertion(
            token,
            secret,
            expected={
                "issuer_skill_id": skill_id,
                "execution_id": str(command.get("execution_id") or ""),
                "audience_provider_id": self.provider_id,
                "provider_instance_id": self.provider_instance_id,
                "provider_boot_id": self.provider_boot_id,
                "assembly_fingerprint": self.assembly_fingerprint,
                "mounted_effector_revision": self.mounted_effector_revision,
                "command_sha256": canonical_sha256(command),
            },
        )
        assertion_id = str(payload["assertion_id"])
        with self.lock:
            now_us = time.time_ns() // 1000
            self.used_assertion_ids = {
                key: expires
                for key, expires in self.used_assertion_ids.items()
                if expires > now_us
            }
            if assertion_id in self.used_assertion_ids:
                raise AuthorizationError("authorization assertion was already used")
            self.used_assertion_ids[assertion_id] = int(payload["expires_at_us"])
        return payload

    def submit(self, command: dict[str, Any], token: str) -> dict[str, Any]:
        if command.get("schema") != "midbrain.grip_control_command":
            raise ValueError("unsupported grip-control command schema")
        if int(command.get("schema_version", 0)) != 1:
            raise ValueError("unsupported grip-control command schema version")
        self._authorize(command, token)
        with self.operation_lock:
            operation = str(command.get("operation") or "").upper()
            if operation == "SET_POSITION_EFFORT":
                return self._set_position_effort(command)
            if operation == "SET_MIT_POSITION":
                return self._set_mit_position(command)
            if operation == "CONFIRM_CARRY":
                return self._confirm_carry(command)
            if operation == "RELEASE_OBJECT":
                return self._release_object(command)
            if operation == "ENTER_MIT_FLOAT":
                return self._enter_mit_float(command)
            raise ValueError(f"unsupported grip operation {operation}")

    def _thermal(self, state: dict[str, Any]) -> dict[str, Any]:
        values = state.get("temperatures_c")
        maximum_age_ms = float(self.config["thermal"]["maximum_feedback_age_ms"])
        age = float(state.get("feedback_age_ms", math.inf))
        valid = isinstance(values, list) and age <= maximum_age_ms
        temperatures: list[Any] = []
        hot: list[int] = []
        unavailable: list[int] = []
        gate_c = float(self.config["thermal"]["new_grip_gate_c"])
        for index in self.active_joint_indices:
            value = values[index] if valid and index < len(values) else None
            temperatures.append(value)
            try:
                finite = value is not None and math.isfinite(float(value))
            except (TypeError, ValueError):
                finite = False
            if not finite:
                unavailable.append(index)
            elif float(value) >= gate_c:
                hot.append(index)
        return {
            "ready_for_new_grip": not hot and not unavailable,
            "gate_c": gate_c,
            "retry_after_s": float(self.config["thermal"]["retry_after_s"]),
            "hot_joint_indices": hot,
            "unavailable_joint_indices": unavailable,
            "active_joint_temperatures_c": temperatures,
            "feedback_age_ms": age,
        }

    def _require_thermal_gate(self, state: dict[str, Any]) -> None:
        thermal = self._thermal(state)
        if thermal["ready_for_new_grip"]:
            return
        raise ThermalGateError(
            "new grip rejected until every active joint temperature is available and below the gate",
            thermal["retry_after_s"],
            thermal["active_joint_temperatures_c"],
        )

    def _limits(self) -> dict[str, float]:
        joint = self.profile["joint_control"]
        return {
            "min_position": float(joint["operational_position_range_rad"][0]),
            "max_position": float(joint["operational_position_range_rad"][1]),
            "max_velocity": float(joint["maximum_velocity_rad_s"]),
            "max_torque": float(joint["maximum_torque_limit_nm"]),
        }

    def _validate_target(self, command: dict[str, Any]) -> GripTarget:
        limits = self._limits()
        target = GripTarget(
            position_rad=float(command["position_rad"]),
            velocity_limit_rad_s=float(command.get("velocity_limit_rad_s", self.profile["joint_control"]["default_velocity_rad_s"])),
            torque_limit_nm=float(command["torque_limit_nm"]),
            intent=str(command.get("intent") or "GRIP").upper(),
        )
        if target.intent not in {"GRIP", "EXPAND", "OPEN"}:
            raise ValueError("intent must be GRIP, EXPAND, or OPEN")
        if not limits["min_position"] <= target.position_rad <= limits["max_position"]:
            raise ValueError("position_rad is outside the effector operational range")
        if not 0.0 < target.velocity_limit_rad_s <= limits["max_velocity"]:
            raise ValueError("velocity_limit_rad_s is outside the effector limit")
        if not 0.0 < target.torque_limit_nm <= limits["max_torque"]:
            raise ValueError("torque_limit_nm is outside the effector limit")
        return target

    def _is_functionally_open(self, position_rad: float) -> bool:
        joint = self.profile["joint_control"]
        target = float(joint["open_position_rad"])
        tolerance = float(joint["open_position_tolerance_rad"])
        direction = str(joint["closing_direction"])
        if direction == "INCREASING_POSITION_RAD":
            return float(position_rad) <= target + tolerance
        if direction == "DECREASING_POSITION_RAD":
            return float(position_rad) >= target - tolerance
        raise RuntimeError("effector closing direction is unsupported")

    @staticmethod
    def _mit_gains(command: dict[str, Any], profile: dict[str, Any]) -> tuple[float, float]:
        kp = float(command.get("kp", profile["kp"]))
        kd = float(command.get("kd", profile["kd"]))
        if not math.isfinite(kp) or not math.isfinite(kd) or kp < 0.0 or kd < 0.0:
            raise ValueError("MIT gains must be finite and non-negative")
        return kp, kd

    def _ensure_lease(self) -> None:
        if self.basic.lease_snapshot() is None:
            self.basic.acquire(
                f"{self.provider_id}:background-grip",
                int(self.config["basic"]["lease_duration_ms"]),
            )

    def _send_target(self, target: GripTarget) -> None:
        self.basic.command(
            POS_TOR,
            {
                "position_rad": target.position_rad,
                "velocity_limit_rad_s": target.velocity_limit_rad_s,
                "torque_limit_nm": target.torque_limit_nm,
            },
            int(self.config["basic"]["command_timeout_ms"]),
        )

    def _set_position_effort(self, command: dict[str, Any]) -> dict[str, Any]:
        target = self._validate_target(command)
        state = self.basic.state()
        with self.lock:
            carrying = self.carry is not None
        if target.intent in {"GRIP", "EXPAND"} and not carrying:
            self._require_thermal_gate(state)
        self._ensure_lease()
        self._send_target(target)
        self.basic.set_required_command_mode(POS_TOR)
        with self.lock:
            self.target = target
            self.float_transition = None
            self.mit_position_transition = None
            self.mit_position_hold = None
            self.contact_samples = 0
            self.contact_inferred = False
            self.state = "CARRYING_POS_TOR" if self.carry else "POS_TOR"
            self.last_command_monotonic = time.monotonic()
        return {"accepted": True, "state": self.state, "thermal": self._thermal(state)}

    def _set_mit_position(self, command: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            if self.carry is not None:
                raise PermissionError("MIT position motion is forbidden while carrying")
        intent = str(command.get("intent") or "").upper()
        if intent != "OPEN":
            raise ValueError("SET_MIT_POSITION currently permits only OPEN intent")
        limits = self._limits()
        position = float(command["position_rad"])
        if not math.isfinite(position) or not (
            limits["min_position"] <= position <= limits["max_position"]
        ):
            raise ValueError("position_rad is outside the effector operational range")
        if not self._is_functionally_open(position):
            raise ValueError("SET_MIT_POSITION OPEN target is not functionally open")
        state = self.basic.state()
        self._require_thermal_gate(state)
        positions = state.get("positions_rad")
        if not isinstance(positions, list) or self.gripper_joint_index >= len(positions):
            raise RuntimeError("measured gripper position is unavailable")
        measured = float(positions[self.gripper_joint_index])
        if not math.isfinite(measured):
            raise RuntimeError("measured gripper position is unavailable")
        profile = self.profile["mit_position_transition"]
        requested_duration = float(
            command.get("duration_s", profile["default_duration_s"])
        )
        maximum_duration = float(profile["maximum_duration_s"])
        if not math.isfinite(requested_duration) or not (
            0.02 <= requested_duration <= maximum_duration
        ):
            raise ValueError("duration_s is outside the effector transition limit")
        kp, kd = self._mit_gains(command, profile)
        required_duration = abs(position - measured) / limits["max_velocity"]
        resolved_duration = max(requested_duration, required_duration)
        if resolved_duration > maximum_duration + 1e-9:
            raise ValueError(
                "MIT position target cannot respect the effector velocity and duration limits"
            )
        rate = max(0.02, abs(position - measured) / resolved_duration)
        self._ensure_lease()
        self.basic.set_required_command_mode(None)
        transition = MitPositionTransition(
            started_monotonic=time.monotonic(),
            requested_duration_s=requested_duration,
            resolved_duration_s=resolved_duration,
            start_position_rad=measured,
            end_position_rad=position,
            target_rate_limit_rad_s=rate,
            kp=kp,
            kd=kd,
        )
        with self.lock:
            self.target = None
            self.float_transition = None
            self.mit_position_hold = None
            self.mit_position_transition = transition
            self.contact_samples = 0
            self.contact_inferred = False
            self.state = "MIT_POSITION_TRANSITION"
            self.last_command_monotonic = time.monotonic()
        return {
            "accepted": True,
            "state": self.state,
            "target_position_rad": position,
            "requested_duration_s": requested_duration,
            "resolved_duration_s": resolved_duration,
            "target_rate_limit_rad_s": rate,
            "thermal": self._thermal(state),
        }

    def _confirm_carry(self, command: dict[str, Any]) -> dict[str, Any]:
        carry_id = str(command.get("carry_id") or "").strip()
        revision = str(command.get("attachment_revision") or "").strip()
        if not carry_id or not revision:
            raise ValueError("carry_id and attachment_revision are required")
        with self.lock:
            if not self.contact_inferred:
                raise RuntimeError("gripper contact has not been inferred")
            if self.target is None:
                raise RuntimeError("gripper has no POSITION_EFFORT_LIMITED target")
        contact = self.contact_http.get(
            f"{self.config['contact']['url'].rstrip('/')}/v1/contact/state"
        )
        contact_carry = contact.get("carry")
        if not isinstance(contact_carry, dict) or not contact_carry.get("confirmed"):
            raise RuntimeError("Contact Provider has not confirmed carrying")
        if contact_carry.get("carry_id") != carry_id or contact_carry.get("attachment_revision") != revision:
            raise RuntimeError("Contact Provider carry identity does not match")
        state = self.basic.state()
        modes = state.get("active_command_modes")
        if not isinstance(modes, list) or any(
            index >= len(modes) or modes[index] != POS_TOR
            for index in self.active_joint_indices
        ):
            raise RuntimeError("all active joints must already be POSITION_EFFORT_LIMITED")
        attachment = command.get("attachment")
        if not isinstance(attachment, dict):
            raise ValueError("attachment must be an object")
        object_binding = attachment.get("object_binding")
        if not isinstance(object_binding, dict) or not object_binding:
            raise ValueError("attachment.object_binding must be a non-empty object")
        for field in ("payload", "collision_geometry"):
            if attachment.get(field) is not None and not isinstance(
                attachment[field], dict
            ):
                raise ValueError(f"attachment.{field} must be an object or null")
        with self.lock:
            self.carry = {
                "carry_id": carry_id,
                "attachment_revision": revision,
                "object_binding": object_binding,
                "payload": attachment.get("payload"),
                "collision_geometry": attachment.get("collision_geometry"),
                "confirmed_at_us": time.time_ns() // 1000,
            }
            self.state = "CARRYING_POS_TOR"
        return {"confirmed": True, "carry": dict(self.carry), "state": self.state}

    def _release_object(self, command: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            carry = None if self.carry is None else dict(self.carry)
        if carry is None:
            raise RuntimeError("no carried object is bound")
        if str(command.get("carry_id") or "") != carry["carry_id"]:
            raise RuntimeError("release carry_id does not match")
        release_target = {
            **command,
            "position_rad": float(command.get("position_rad", self.profile["joint_control"]["open_position_rad"])),
            "torque_limit_nm": float(command.get("torque_limit_nm", self.profile["joint_control"]["release_torque_limit_nm"])),
            "intent": "OPEN",
        }
        target = self._validate_target(release_target)
        self._ensure_lease()
        self._send_target(target)
        self.basic.set_required_command_mode(POS_TOR)
        with self.lock:
            self.target = target
            self.float_transition = None
            self.mit_position_transition = None
            self.mit_position_hold = None
            self.carry = None
            self.contact_inferred = False
            self.contact_samples = 0
            self.state = "RELEASING_POS_TOR"
        return {
            "accepted": True,
            "released_carry": carry,
            "target_position_rad": target.position_rad,
            "state": self.state,
        }

    def _enter_mit_float(self, command: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            if self.carry is not None:
                raise PermissionError("MIT float is forbidden while carrying")
        state = self.basic.state()
        positions = state.get("positions_rad")
        if not isinstance(positions, list) or self.gripper_joint_index >= len(positions):
            raise RuntimeError("measured gripper position is unavailable")
        measured = float(positions[self.gripper_joint_index])
        duration = float(command.get("delta_time_s", self.profile["mit_float_transition"]["default_delta_time_s"]))
        if not 0.02 <= duration <= float(self.profile["mit_float_transition"]["maximum_delta_time_s"]):
            raise ValueError("delta_time_s is outside the effector transition limit")
        kp, kd = self._mit_gains(command, self.profile["mit_float_transition"])
        with self.lock:
            start = self.target.position_rad if self.target is not None else measured
        self._ensure_lease()
        self.basic.set_required_command_mode(None)
        transition = FloatTransition(
            time.monotonic(),
            duration,
            start,
            measured,
            kp,
            kd,
        )
        with self.lock:
            self.float_transition = transition
            self.target = None
            self.mit_position_transition = None
            self.mit_position_hold = None
            self.state = "MIT_FLOAT_TRANSITION"
        return {"accepted": True, "state": self.state, "delta_time_s": duration}

    def _finish_float(self, reason: str) -> None:
        lease = self.basic.lease_snapshot()
        if lease is not None:
            if lease.required_command_mode is not None:
                self.basic.set_required_command_mode(None)
            self.basic.float(reason)
            self.basic.release(reason)
        with self.lock:
            self.target = None
            self.float_transition = None
            self.mit_position_transition = None
            self.mit_position_hold = None
            self.state = "MIT_FLOAT"

    def _update_contact_inference(self, state: dict[str, Any]) -> None:
        torques = state.get("torques_nm", [])
        index = self.gripper_joint_index
        if index >= len(torques):
            self.contact_samples = 0
            return
        measured_torque = float(torques[index])
        if not math.isfinite(measured_torque):
            self.contact_samples = 0
            return
        detection = self.profile["contact_detection"]
        loaded = abs(measured_torque) >= float(
            detection["minimum_absolute_measured_torque_nm"]
        )
        self.contact_samples = self.contact_samples + 1 if loaded else 0
        if self.contact_samples >= int(detection["stable_samples_at_50hz"]):
            self.contact_inferred = True
            if self.carry is None:
                self.state = "CONTACT_INFERRED"

    def _audit_carry_modes(self, state: dict[str, Any]) -> None:
        with self.lock:
            carrying = self.carry is not None
        if not carrying:
            return
        modes = state.get("active_command_modes")
        bad = not isinstance(modes, list) or any(
            index >= len(modes) or modes[index] != POS_TOR
            for index in self.active_joint_indices
        )
        with self.lock:
            if bad:
                self.health = "DEGRADED"
                self.state = "DEGRADED"
                self.last_error = "carrying mode invariant violated: every active joint must remain POSITION_EFFORT_LIMITED"
            elif self._thermal(state)["hot_joint_indices"]:
                self.health = "DEGRADED"
                self.state = "THERMAL_RELEASE_RECOMMENDED"
                self.last_error = "one or more active joints reached the new-grip thermal gate while carrying"
            else:
                self.health = "HEALTHY"
                self.state = "CARRYING_POS_TOR"
                self.last_error = None

    def _send_mit_position(
        self,
        *,
        position_rad: float,
        target_rate_limit_rad_s: float,
        kp: float,
        kd: float,
    ) -> None:
        self.basic.command(
            MIT,
            {
                "position_rad": float(position_rad),
                "velocity_rad_s": 0.0,
                "target_rate_limit_rad_s": float(target_rate_limit_rad_s),
                "kp": float(kp),
                "kd": float(kd),
                "feedforward_torque_nm": 0.0,
            },
            int(self.config["basic"]["command_timeout_ms"]),
        )

    def _tick(self) -> None:
        state = self.basic.state()
        with self.lock:
            self.last_feedback = state
            transition = self.float_transition
            position_transition = self.mit_position_transition
            position_hold = self.mit_position_hold
            target = self.target
        if transition is not None:
            elapsed = time.monotonic() - transition.started_monotonic
            ratio = min(1.0, max(0.0, elapsed / transition.duration_s))
            position = transition.start_position_rad + ratio * (
                transition.end_position_rad - transition.start_position_rad
            )
            self.basic.command(
                MIT,
                {
                    "position_rad": position,
                    "velocity_rad_s": 0.0,
                    "target_rate_limit_rad_s": max(
                        0.02,
                        abs(transition.end_position_rad - transition.start_position_rad)
                        / transition.duration_s,
                    ),
                    "kp": transition.kp,
                    "kd": transition.kd,
                    "feedforward_torque_nm": 0.0,
                },
                int(self.config["basic"]["command_timeout_ms"]),
            )
            if ratio >= 1.0:
                self._finish_float("completed 50 Hz MIT float transition")
            return
        if position_transition is not None:
            elapsed = time.monotonic() - position_transition.started_monotonic
            ratio = min(
                1.0,
                max(0.0, elapsed / position_transition.resolved_duration_s),
            )
            position = position_transition.start_position_rad + ratio * (
                position_transition.end_position_rad
                - position_transition.start_position_rad
            )
            self._send_mit_position(
                position_rad=position,
                target_rate_limit_rad_s=position_transition.target_rate_limit_rad_s,
                kp=position_transition.kp,
                kd=position_transition.kd,
            )
            if ratio >= 1.0:
                with self.lock:
                    self.mit_position_transition = None
                    self.mit_position_hold = MitPositionHold(
                        position_rad=position_transition.end_position_rad,
                        target_rate_limit_rad_s=position_transition.target_rate_limit_rad_s,
                        kp=position_transition.kp,
                        kd=position_transition.kd,
                    )
                    self.state = "MIT_POSITION_HOLD"
            return
        if target is not None:
            self._send_target(target)
            with self.lock:
                if target.intent in {"GRIP", "EXPAND"}:
                    self._update_contact_inference(state)
            self._audit_carry_modes(state)
            return
        if position_hold is not None:
            self._send_mit_position(
                position_rad=position_hold.position_rad,
                target_rate_limit_rad_s=position_hold.target_rate_limit_rad_s,
                kp=position_hold.kp,
                kd=position_hold.kd,
            )

    def _loop(self) -> None:
        next_tick = time.monotonic()
        next_renew = next_tick
        while not self.stop_event.is_set():
            now = time.monotonic()
            try:
                with self.operation_lock:
                    if now >= next_renew and self.basic.lease_snapshot() is not None:
                        self.basic.renew(int(self.config["basic"]["lease_duration_ms"]))
                        next_renew = now + float(self.config["basic"]["lease_renewal_interval_ms"]) / 1000.0
                    self._tick()
            except Exception as exc:
                with self.lock:
                    self.health = "DEGRADED"
                    self.state = "DEGRADED"
                    self.last_error = str(exc)
            next_tick += self.period_s
            delay = next_tick - time.monotonic()
            if delay <= -self.period_s:
                next_tick = time.monotonic()
                delay = 0.0
            self.stop_event.wait(max(0.0, delay))

    def snapshot(self) -> dict[str, Any]:
        try:
            latest = self.basic.state() if self.initialized else {}
        except Exception:
            latest = self.last_feedback
        with self.lock:
            thermal = self._thermal(latest) if self.initialized else None
            lease = self.basic.lease_snapshot()
            positions = latest.get("positions_rad") if isinstance(latest, dict) else None
            velocities = latest.get("velocities_rad_s") if isinstance(latest, dict) else None
            gripper_position = (
                positions[self.gripper_joint_index]
                if isinstance(positions, list)
                and 0 <= self.gripper_joint_index < len(positions)
                else None
            )
            functionally_open = False
            try:
                functionally_open = (
                    gripper_position is not None
                    and math.isfinite(float(gripper_position))
                    and self._is_functionally_open(float(gripper_position))
                )
            except (TypeError, ValueError, RuntimeError):
                functionally_open = False
            ready_for_approach = bool(
                self.state == "MIT_POSITION_HOLD"
                and functionally_open
                and isinstance(thermal, dict)
                and thermal.get("ready_for_new_grip") is True
            )
            return {
                "provider_id": self.provider_id,
                "provider_instance_id": self.provider_instance_id,
                "provider_boot_id": self.provider_boot_id,
                "residency": self.residency,
                "health": self.health,
                "ready": self.initialized and self.health != "STARTING",
                "state": self.state,
                "last_error": self.last_error,
                "assembly_fingerprint": self.assembly_fingerprint,
                "mounted_effector_revision": self.mounted_effector_revision,
                "gripper_resource_id": self.gripper_resource_id,
                "gripper_joint_index": self.gripper_joint_index,
                "gripper_position_rad": gripper_position,
                "gripper_velocity_rad_s": (
                    velocities[self.gripper_joint_index]
                    if isinstance(velocities, list)
                    and 0 <= self.gripper_joint_index < len(velocities)
                    else None
                ),
                "gripper_torque_nm": (
                    latest.get("torques_nm", [])[self.gripper_joint_index]
                    if isinstance(latest.get("torques_nm"), list)
                    and 0 <= self.gripper_joint_index < len(latest["torques_nm"])
                    else None
                ),
                "active_joint_indices": list(self.active_joint_indices),
                "control_rate_hz": float(self.config["control"]["rate_hz"]),
                "target": None if self.target is None else self.target.__dict__.copy(),
                "mit_position_transition": (
                    None
                    if self.mit_position_transition is None
                    else self.mit_position_transition.__dict__.copy()
                ),
                "mit_position_hold": (
                    None
                    if self.mit_position_hold is None
                    else self.mit_position_hold.__dict__.copy()
                ),
                "functionally_open": functionally_open,
                "ready_for_approach": ready_for_approach,
                "contact_inferred": self.contact_inferred,
                "contact_stable_samples": self.contact_samples,
                "carry": None if self.carry is None else dict(self.carry),
                "thermal": thermal,
                "basic_lease": None if lease is None else lease.__dict__.copy(),
                "all_active_joints_position_effort_limited": bool(
                    latest.get("active_command_modes")
                    and all(
                        index < len(latest["active_command_modes"])
                        and latest["active_command_modes"][index] == POS_TOR
                        for index in self.active_joint_indices
                    )
                ),
            }
