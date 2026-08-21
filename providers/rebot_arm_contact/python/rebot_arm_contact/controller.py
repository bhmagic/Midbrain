from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import copy
import math
import os
import threading
import time

import numpy as np

from .authorization import (
    AuthorizationError,
    canonical_sha256,
    verify_assertion,
)
from .basic_client import BasicControllerClient
from .kinematics import (
    ContactKinematics,
    matrix_quaternion,
    rotation_vector,
    rpy_matrix,
    transform,
)


@dataclass
class ContactSession:
    session_id: str
    plan: dict[str, Any]
    assertion_id: str
    authorization_expires_at_us: int
    started_at_us: int
    active_sequence: int = -1
    deadline_monotonic: float = 0.0


@dataclass
class ActiveEndpoint:
    sequence: int
    step: dict[str, Any]
    q_goal: np.ndarray
    locked_indices: tuple[int, ...]
    accepted_at_us: int
    position_residual_m: float
    orientation_residual_rad: float
    velocity_limits_rad_s: np.ndarray
    velocity_limited_transition_time_s: float
    motion_type: str
    q_command: np.ndarray
    resolved_target: dict[str, Any]
    segment: SegmentTrajectory | None = None


@dataclass
class SegmentTrajectory:
    q_waypoints: np.ndarray
    time_waypoints_s: np.ndarray
    started_monotonic: float
    cartesian_distance_m: float
    maximum_position_residual_m: float
    maximum_orientation_residual_rad: float
    start_position_m: np.ndarray
    goal_position_m: np.ndarray
    goal_rotation: np.ndarray
    command_updates_sent: int = 0
    progress: float = 0.0
    complete: bool = False
    measured_samples: int = 0
    maximum_commanded_cross_track_error_m: float = 0.0
    maximum_commanded_orientation_error_rad: float = 0.0
    maximum_measured_cross_track_error_m: float = 0.0
    maximum_measured_orientation_error_rad: float = 0.0
    maximum_measured_joint_tracking_error_rad: float = 0.0
    last_measured_along_track_fraction: float = 0.0


def _six(values: Any, name: str) -> np.ndarray:
    result = np.asarray(values, dtype=float)
    if result.shape != (6,) or not np.all(np.isfinite(result)):
        raise ValueError(f"{name} must contain six finite values")
    return result


def _basic_position_effort_limits(
    model: dict[str, Any],
) -> tuple[np.ndarray, np.ndarray]:
    raw = model.get("command_limits", {}).get("POSITION_EFFORT_LIMITED")
    if not isinstance(raw, list) or len(raw) < 6:
        raise RuntimeError(
            "Basic model does not expose six POSITION_EFFORT_LIMITED command limits"
        )
    arm = raw[:6]
    if any(
        not isinstance(item, dict)
        or int(item.get("joint_index", -1)) != index
        for index, item in enumerate(arm)
    ):
        raise RuntimeError("Basic POSITION_EFFORT_LIMITED limits are not joint ordered")
    velocities = _six(
        [item.get("velocity_limit_rad_s") for item in arm],
        "Basic POSITION_EFFORT_LIMITED velocity limits",
    )
    torques = _six(
        [item.get("torque_limit_nm") for item in arm],
        "Basic POSITION_EFFORT_LIMITED torque limits",
    )
    if np.any(velocities <= 0.0) or np.any(torques <= 0.0):
        raise RuntimeError("Basic POSITION_EFFORT_LIMITED limits must be positive")
    return velocities, torques


class ContactController:
    """Persistent contact control owner above Basic and below finite Skills."""

    def __init__(
        self,
        config: dict[str, Any],
        basic: BasicControllerClient,
        *,
        provider_instance_id: str,
        provider_boot_id: str,
    ):
        self.config = copy.deepcopy(config)
        self.basic = basic
        self.provider_id = str(config["provider_id"])
        self.provider_instance_id = provider_instance_id
        self.provider_boot_id = provider_boot_id
        self.lock = threading.RLock()
        self.operation_lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.state_thread: threading.Thread | None = None
        self.kinematics: ContactKinematics | None = None
        self.basic_model: dict[str, Any] = {}
        self.basic_velocity_limits_rad_s: np.ndarray | None = None
        self.basic_torque_limits_nm: np.ndarray | None = None
        self.basic_control_rate_hz: float | None = None
        self.basic_assembly: dict[str, Any] = {}
        self.basic_state: dict[str, Any] = {}
        self.assembly_fingerprint: str | None = None
        self.mounted_effector_revision: str | None = None
        self.acting_frame_id: str | None = None
        self.arm_resource_id: str | None = None
        self.session: ContactSession | None = None
        self.endpoint: ActiveEndpoint | None = None
        self.lock_positions: dict[int, float] = {}
        self.consumed_assertion_ids: set[str] = set()
        self.health = "STARTING"
        self.ready = False
        self.requested_residency = "WARM"
        self.residency = "WARM"
        self.control_state = "INITIALIZING"
        self.last_disposition = "NONE"
        self.last_error: str | None = None
        self.last_control_fault: str | None = None
        self.last_relax_reason: str | None = None
        self.torque_limits_nm: np.ndarray | None = None
        self.gravity_budget_nm: np.ndarray | None = None
        self.wrench_budget_nm: np.ndarray | None = None
        self.saturated_joint_indices: list[int] = []
        self.last_command_at_us: int | None = None
        self.last_state_success_monotonic = 0.0
        self.next_basic_lease_renewal_monotonic = 0.0
        self.float_confirmed = False
        self.motion_inhibited = False
        self.carry_id: str | None = None
        self.carry_attachment_revision: str | None = None
        self.carry_confirmed = False
        self.position_effort_guard_active = False

    def start(self) -> None:
        with self.operation_lock:
            self._refresh_runtime_binding()
        self.stop_event.clear()
        self.thread = threading.Thread(
            target=self._control_loop,
            name="contact-work-control",
            daemon=True,
        )
        self.state_thread = threading.Thread(
            target=self._state_loop,
            name="contact-work-state",
            daemon=True,
        )
        self.state_thread.start()
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        with self.operation_lock:
            self._relax("Contact Provider shutdown", "SHUTDOWN_RELAXED")
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=2.0)
        if (
            self.state_thread
            and self.state_thread is not threading.current_thread()
        ):
            self.state_thread.join(timeout=2.0)
        with self.lock:
            self.ready = False
            self.residency = "COLD"
            self.control_state = "STOPPED"

    def set_motion_inhibited(self, inhibited: bool, reason: str) -> None:
        with self.operation_lock:
            changed = bool(inhibited) != self.motion_inhibited
            self.motion_inhibited = bool(inhibited)
            if changed and inhibited and self.session is not None:
                if self.carry_confirmed:
                    self._hold_carrying(reason, "MOTION_INHIBIT_CARRY_HOLD")
                else:
                    self._relax(reason, "MOTION_INHIBIT_RELAXED")

    def enter_hot(self) -> dict[str, Any]:
        with self.operation_lock:
            if self.stop_event.is_set():
                raise RuntimeError("Contact Work Provider is stopping")
            self._assert_assembly_unchanged()
            self._fresh_state()
            self.requested_residency = "HOT"
            self.residency = "HOT"
            if self.session is None:
                self.control_state = "HOT_READY"
            return self.snapshot()

    def enter_warm(self) -> dict[str, Any]:
        with self.operation_lock:
            if self.carry_confirmed:
                raise RuntimeError(
                    "Contact Provider cannot enter WARM during a confirmed carry"
                )
            self.requested_residency = "WARM"
            if self.session is not None or self.basic.lease_snapshot() is not None:
                self._relax("Contact Provider entering WARM", "WARM_RELAXED")
            self.residency = "WARM"
            self.control_state = "WARM_READY"
            return self.snapshot()

    def _refresh_runtime_binding(self) -> None:
        model = self.basic.model()
        assembly = self.basic.assembly()
        state = self.basic.state()
        if assembly.get("schema") != "midbrain.robot_assembly_state":
            raise RuntimeError("Basic returned an unsupported assembly state")
        fingerprint = str(assembly.get("assembly_fingerprint") or "").strip()
        if len(fingerprint) != 64:
            raise RuntimeError("assembly fingerprint is missing or invalid")
        roles = assembly.get("qualified_control_roles")
        roles = roles if isinstance(roles, dict) else {}
        contact_role = roles.get("contact")
        if not isinstance(contact_role, dict):
            raise RuntimeError("selected assembly does not qualify contact control")
        required = str(contact_role.get("required_capability") or "")
        if required != "robot_arm.motion.contact.position_effort_limited.v1":
            raise RuntimeError("selected assembly qualifies an unsupported contact role")
        groups = assembly.get("resource_groups")
        groups = groups if isinstance(groups, list) else []
        arm_groups = [
            item
            for item in groups
            if isinstance(item, dict) and item.get("group_id") == "arm"
        ]
        if len(arm_groups) != 1:
            raise RuntimeError("assembly must expose exactly one arm group")
        joint_names = arm_groups[0].get("joint_names")
        if not isinstance(joint_names, list) or len(joint_names) != 6:
            raise RuntimeError("contact control requires a six-joint arm group")
        resource_id = str(arm_groups[0].get("resource_id") or "").strip()
        effector = assembly.get("mounted_effector")
        effector = effector if isinstance(effector, dict) else {}
        revision = str(effector.get("profile_revision") or "").strip()
        controlled = effector.get("controlled_frame")
        controlled = controlled if isinstance(controlled, dict) else {}
        acting_frame_id = str(controlled.get("frame_id") or "").strip()
        controlled_transform = controlled.get("transform")
        controlled_transform = (
            controlled_transform if isinstance(controlled_transform, dict) else {}
        )
        xyz = np.asarray(controlled_transform.get("translation_m", []), dtype=float)
        rpy = np.asarray(controlled_transform.get("rpy_rad", []), dtype=float)
        if (
            not resource_id
            or not revision
            or not acting_frame_id
            or xyz.shape != (3,)
            or rpy.shape != (3,)
            or not np.all(np.isfinite(xyz))
            or not np.all(np.isfinite(rpy))
        ):
            raise RuntimeError("assembly arm group or controlled frame is invalid")
        self.basic.bind_resource(resource_id)
        kinematics = ContactKinematics(
            model, transform(xyz, rpy_matrix(rpy))
        )
        velocity_limits, torque_limits = _basic_position_effort_limits(model)
        try:
            basic_control_rate_hz = float(model["control"]["internal_rate_hz"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Basic model does not publish its internal control rate") from exc
        if (
            not math.isfinite(basic_control_rate_hz)
            or basic_control_rate_hz <= 0.0
            or basic_control_rate_hz > 1000.0
        ):
            raise RuntimeError("Basic internal control rate is outside (0, 1000] Hz")
        if kinematics.joint_names != [str(name) for name in joint_names]:
            raise RuntimeError("assembly arm group does not match Basic joint order")
        with self.lock:
            self.basic_model = copy.deepcopy(model)
            self.basic_velocity_limits_rad_s = velocity_limits.copy()
            self.basic_torque_limits_nm = torque_limits.copy()
            self.basic_control_rate_hz = basic_control_rate_hz
            self.basic_assembly = copy.deepcopy(assembly)
            self.assembly_fingerprint = fingerprint
            self.mounted_effector_revision = revision
            self.acting_frame_id = acting_frame_id
            self.arm_resource_id = resource_id
            self.kinematics = kinematics
        self._accept_fresh_state(state)
        with self.lock:
            self.health = "HEALTHY"
            self.control_state = "WARM_READY"

    def _assert_assembly_unchanged(self) -> None:
        current = self.basic.assembly()
        actual = str(current.get("assembly_fingerprint") or "").strip()
        if actual != self.assembly_fingerprint:
            raise RuntimeError("selected robot assembly changed during contact work")

    def _validate_plan(self, plan: dict[str, Any]) -> None:
        if not isinstance(plan, dict):
            raise ValueError("Contact Work plan must be a JSON object")
        schema_version = plan.get("schema_version")
        if (
            plan.get("schema") != "midbrain.contact_work_plan"
            or schema_version not in {1, 2}
        ):
            raise ValueError("unsupported Contact Work plan schema")
        expected_fields = {
            "schema",
            "schema_version",
            "plan_id",
            "skill_id",
            "execution_id",
            "provider_id",
            "assembly_fingerprint",
            "acting_frame_id",
            "manager_authority",
            "steps",
        }
        if schema_version == 2:
            expected_fields.add("carry")
        if set(plan) != expected_fields:
            raise ValueError(
                f"Contact Work plan fields do not match schema version {schema_version}"
            )
        required_text = (
            "plan_id",
            "skill_id",
            "execution_id",
            "provider_id",
            "assembly_fingerprint",
            "acting_frame_id",
        )
        for name in required_text:
            if not str(plan.get(name) or "").strip():
                raise ValueError(f"plan is missing {name}")
        if plan["provider_id"] != self.provider_id:
            raise ValueError("plan targets another Provider")
        if plan["assembly_fingerprint"] != self.assembly_fingerprint:
            raise ValueError("plan assembly fingerprint is stale")
        if plan["acting_frame_id"] != self.acting_frame_id:
            raise ValueError("plan acting frame does not match the selected assembly")
        manager_authority = plan.get("manager_authority")
        if not isinstance(manager_authority, dict):
            raise ValueError("plan is missing Manager authority lineage")
        if set(manager_authority) != {
            "resource_id",
            "lease_id",
            "owner_id",
            "fencing_generation",
            "permissions",
        }:
            raise ValueError("Manager authority lineage fields are invalid")
        if manager_authority.get("resource_id") != self.arm_resource_id:
            raise ValueError("Manager authority targets another resource")
        if manager_authority.get("owner_id") != plan["execution_id"]:
            raise ValueError("Manager authority owner does not match execution_id")
        if not str(manager_authority.get("lease_id") or "").strip():
            raise ValueError("Manager authority lease_id is missing")
        manager_generation = manager_authority.get("fencing_generation")
        if (
            not isinstance(manager_generation, int)
            or isinstance(manager_generation, bool)
            or manager_generation < 1
        ):
            raise ValueError("Manager authority fencing generation is invalid")
        permissions = manager_authority.get("permissions")
        if not isinstance(permissions, list) or not {
            "execute_contact",
            "relax",
        }.issubset({str(value) for value in permissions}):
            raise ValueError("Manager authority permissions are incomplete")
        secret_envs = self.config["authorization"].get("skill_secret_envs", {})
        allowed = {str(value) for value in secret_envs}
        if plan["skill_id"] not in allowed:
            raise AuthorizationError("Contact Skill identity is not allowlisted")
        if schema_version == 2:
            carry = plan.get("carry")
            if not isinstance(carry, dict) or set(carry) != {
                "behavior",
                "carry_id",
                "attachment_revision",
            }:
                raise ValueError("Contact carry binding fields are invalid")
            if carry.get("behavior") not in {"PREPARE", "CONTINUE"}:
                raise ValueError("Contact carry behavior is unsupported")
            if not str(carry.get("carry_id") or "").strip():
                raise ValueError("Contact carry_id is missing")
            if not str(carry.get("attachment_revision") or "").strip():
                raise ValueError("Contact attachment revision is missing")
        steps = plan.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ValueError("plan must contain at least one step")
        if len(steps) > int(self.config["limits"]["maximum_plan_steps"]):
            raise ValueError("plan has too many contact steps")
        if self.kinematics is None:
            raise RuntimeError("kinematics are unavailable")
        joint_names = set(self.kinematics.joint_names)
        maximum_wait = float(self.config["limits"]["maximum_wait_timeout_s"])
        root_frame = self.kinematics.root_frame_id
        for expected_sequence, step in enumerate(steps):
            if not isinstance(step, dict) or set(step) != {
                "sequence",
                "motion_type",
                "target",
                "wrench",
                "locked_joint_names",
                "delay_after_accept_s",
                "next_command_timeout_s",
            }:
                raise ValueError("plan step fields do not match schema version 1")
            if step.get("motion_type") not in {"ONE_SHOT", "CARTESIAN_SEGMENT"}:
                raise ValueError("plan step motion_type is unsupported")
            if (
                not isinstance(step.get("sequence"), int)
                or isinstance(step.get("sequence"), bool)
                or step.get("sequence") != expected_sequence
            ):
                raise ValueError("plan step sequence must be contiguous from zero")
            target = step.get("target")
            wrench = step.get("wrench")
            if not isinstance(target, dict) or not isinstance(wrench, dict):
                raise ValueError("each step requires target and wrench objects")
            if set(target) != {
                "frame_id",
                "position_mode",
                "position_m",
                "orientation_xyzw",
            }:
                raise ValueError("target fields do not match schema version 1")
            if set(wrench) != {"frame_id", "force_n", "torque_nm"}:
                raise ValueError("wrench fields do not match schema version 1")
            if target.get("frame_id") != root_frame:
                raise ValueError("target frame must be the Basic arm root frame")
            if target.get("position_mode") not in {
                "ABSOLUTE_ROOT",
                "RELATIVE_TO_MEASURED_EFFECTOR_ROOT_AXES",
            }:
                raise ValueError("target position_mode is unsupported")
            self.kinematics.target_transform(target)
            force = np.asarray(wrench.get("force_n"), dtype=float)
            torque = np.asarray(wrench.get("torque_nm"), dtype=float)
            if (
                force.shape != (3,)
                or torque.shape != (3,)
                or not np.all(np.isfinite(force))
                or not np.all(np.isfinite(torque))
            ):
                raise ValueError("step wrench must contain six finite components")
            if wrench.get("frame_id") not in {root_frame, self.acting_frame_id}:
                raise ValueError("step wrench frame is unsupported")
            locked = step.get("locked_joint_names")
            if not isinstance(locked, list) or len(locked) != len(set(locked)):
                raise ValueError("locked_joint_names must be a unique array")
            if not set(str(value) for value in locked).issubset(joint_names):
                raise ValueError("step locks an unknown arm joint")
            wait = float(step.get("next_command_timeout_s", 0.0))
            delay = float(step.get("delay_after_accept_s", 0.0))
            if not math.isfinite(wait) or wait <= 0.0 or wait > maximum_wait:
                raise ValueError("step next-command timeout is invalid")
            if not math.isfinite(delay) or delay < 0.0 or delay >= wait:
                raise ValueError("Skill delay must be non-negative and shorter than the watchdog timeout")

    def begin_session(
        self,
        plan: dict[str, Any],
        authorization: str,
    ) -> dict[str, Any]:
        with self.operation_lock:
            if self.stop_event.is_set() or not self.ready:
                raise RuntimeError("Contact Work Provider is not ready")
            if self.residency != "HOT":
                raise RuntimeError("Contact Work Provider must be HOT before a session")
            if self.motion_inhibited:
                raise PermissionError("global motion inhibit is active")
            self._assert_assembly_unchanged()
            self._fresh_state()
            self._validate_plan(plan)
            carry = plan.get("carry") if plan.get("schema_version") == 2 else None
            continuing_carry = bool(
                self.carry_confirmed
                and isinstance(carry, dict)
                and carry.get("behavior") == "CONTINUE"
                and carry.get("carry_id") == self.carry_id
                and carry.get("attachment_revision")
                == self.carry_attachment_revision
            )
            if self.session is not None and not continuing_carry:
                raise RuntimeError("a Contact Work session is already active")
            if (
                isinstance(carry, dict)
                and carry.get("behavior") == "CONTINUE"
                and not continuing_carry
            ):
                raise RuntimeError("Contact carry continuation does not match the active hold")
            plan_sha256 = canonical_sha256(plan)
            secret_name = str(
                self.config["authorization"]["skill_secret_envs"][plan["skill_id"]]
            )
            secret = os.getenv(secret_name, "")
            if len(secret.encode("utf-8")) < 32:
                raise AuthorizationError(
                    f"Contact Skill authorization secret {secret_name} is not configured"
                )
            payload = verify_assertion(
                authorization,
                secret,
                expected={
                    "issuer_skill_id": plan["skill_id"],
                    "execution_id": plan["execution_id"],
                    "audience_provider_id": self.provider_id,
                    "provider_instance_id": self.provider_instance_id,
                    "provider_boot_id": self.provider_boot_id,
                    "assembly_fingerprint": self.assembly_fingerprint,
                    "mounted_effector_revision": self.mounted_effector_revision,
                    "plan_sha256": plan_sha256,
                },
            )
            assertion_id = str(payload["assertion_id"])
            if assertion_id in self.consumed_assertion_ids:
                raise AuthorizationError("Contact Skill authorization was already consumed")
            if continuing_carry:
                lease = self.basic.lease_snapshot()
                if lease is None:
                    raise RuntimeError("confirmed carry lost its Basic arm lease")
            else:
                lease = self.basic.acquire(
                    f"{self.provider_id}:{plan['skill_id']}:{plan['execution_id']}",
                    int(self.config["basic"]["lease_duration_ms"]),
                )
            now_us = time.time_ns() // 1000
            default_wait = float(self.config["limits"]["default_wait_timeout_s"])
            self.session = ContactSession(
                str(plan["execution_id"]),
                copy.deepcopy(plan),
                assertion_id,
                int(payload["expires_at_us"]),
                now_us,
                deadline_monotonic=time.monotonic() + default_wait,
            )
            self.next_basic_lease_renewal_monotonic = (
                time.monotonic()
                + float(
                    self.config["basic"]["lease_renewal_interval_ms"]
                )
                / 1000.0
            )
            if not continuing_carry:
                self.endpoint = None
            self.lock_positions = {}
            self.consumed_assertion_ids.add(assertion_id)
            self.residency = "HOT"
            self.control_state = (
                "CARRYING_WAITING_FOR_SETPOINT"
                if continuing_carry
                else "WAITING_FOR_FIRST_SETPOINT"
            )
            self.last_disposition = (
                "CARRY_SESSION_REPLACED"
                if continuing_carry
                else "SESSION_ACCEPTED"
            )
            self.last_error = None
            self.last_control_fault = None
            self.last_relax_reason = None
            self.float_confirmed = False
            if isinstance(carry, dict) and carry.get("behavior") == "PREPARE":
                self.carry_id = str(carry["carry_id"])
                self.carry_attachment_revision = str(
                    carry["attachment_revision"]
                )
                self.carry_confirmed = False
            return {
                "session_id": self.session.session_id,
                "plan_sha256": plan_sha256,
                "disposition": self.last_disposition,
                "basic_lease_id": lease.lease_id,
                "basic_fencing_generation": lease.fencing_generation,
            }

    def settling_observation(
        self,
        session_id: str,
        sequence: int,
        *,
        maximum_joint_error_rad: float = 0.04,
        maximum_joint_velocity_rad_s: float = 0.05,
    ) -> dict[str, Any]:
        with self.operation_lock:
            if self.session is None or self.session.session_id != str(session_id):
                raise RuntimeError("settling observation does not match the active session")
            if self.endpoint is None or self.endpoint.sequence != int(sequence):
                raise RuntimeError("settling observation does not match the active endpoint")
            position_limit = float(maximum_joint_error_rad)
            velocity_limit = float(maximum_joint_velocity_rad_s)
            if (
                not math.isfinite(position_limit)
                or not math.isfinite(velocity_limit)
                or position_limit <= 0.0
                or velocity_limit <= 0.0
            ):
                raise ValueError("settling limits must be positive finite values")
            state = self._fresh_state()
            positions = _six(state.get("positions_rad", [])[:6], "measured positions")
            velocities = _six(state.get("velocities_rad_s", [])[:6], "measured velocities")
            maximum_error = float(
                np.max(np.abs(positions - self.endpoint.q_goal))
            )
            maximum_velocity = float(np.max(np.abs(velocities)))
            trajectory_complete = bool(
                self.endpoint.segment is None or self.endpoint.segment.complete
            )
            settled = bool(
                trajectory_complete
                and maximum_error <= position_limit
                and maximum_velocity <= velocity_limit
            )
            return {
                "session_id": self.session.session_id,
                "sequence": self.endpoint.sequence,
                "settled": settled,
                "trajectory_complete": trajectory_complete,
                "maximum_joint_error_rad": maximum_error,
                "maximum_joint_velocity_rad_s": maximum_velocity,
                "position_limit_rad": position_limit,
                "velocity_limit_rad_s": velocity_limit,
                "observed_at_us": state.get("observed_at_us"),
            }

    def confirm_carry(
        self,
        session_id: str,
        carry_id: str,
        attachment_revision: str,
    ) -> dict[str, Any]:
        with self.operation_lock:
            session = self.session
            if session is None or session.session_id != str(session_id):
                raise RuntimeError("carry confirmation does not match the active session")
            carry = session.plan.get("carry")
            if not isinstance(carry, dict) or carry.get("behavior") != "PREPARE":
                raise RuntimeError("active Contact plan did not prepare a carry")
            if (
                str(carry_id) != str(carry.get("carry_id"))
                or str(attachment_revision)
                != str(carry.get("attachment_revision"))
            ):
                raise RuntimeError("carry confirmation binding does not match the signed plan")
            if self.endpoint is None or session.active_sequence != len(session.plan["steps"]) - 1:
                raise RuntimeError("all prepared Contact steps must be accepted before carry confirmation")
            settling = self.settling_observation(
                session_id,
                self.endpoint.sequence,
            )
            if not settling["settled"]:
                raise RuntimeError("arm endpoint is not settled for carry confirmation")
            lease = self.basic.lease_snapshot()
            if (
                not self.position_effort_guard_active
                or lease is None
                or lease.required_command_mode != "POSITION_EFFORT_LIMITED"
            ):
                raise RuntimeError(
                    "Contact POSITION_EFFORT_LIMITED mode guard was not retained"
                )
            self.carry_id = str(carry_id)
            self.carry_attachment_revision = str(attachment_revision)
            self.carry_confirmed = True
            session.deadline_monotonic = math.inf
            self.control_state = "CARRYING_POSITION_EFFORT_HOLD"
            self.last_disposition = "CARRY_CONFIRMED"
            return {
                "disposition": "CARRY_CONFIRMED",
                "carry_id": self.carry_id,
                "attachment_revision": self.carry_attachment_revision,
                "required_command_mode": lease.required_command_mode,
                "settling": settling,
            }

    def _hold_carrying(self, reason: str, disposition: str) -> None:
        if not self.carry_confirmed or self.session is None or self.endpoint is None:
            raise RuntimeError("no confirmed carry endpoint is available")
        if self.endpoint.segment is not None and not self.endpoint.segment.complete:
            self.endpoint.segment.complete = True
            self.endpoint.q_goal = self.endpoint.q_command.copy()
        self.session.deadline_monotonic = math.inf
        self.control_state = "CARRYING_POSITION_EFFORT_HOLD"
        self.last_disposition = disposition
        self.last_error = str(reason)

    def move(self, session_id: str, sequence: int) -> dict[str, Any]:
        with self.operation_lock:
            session = self.session
            if session is None or session.session_id != str(session_id):
                detail = (
                    self.last_control_fault
                    or self.last_relax_reason
                    or self.last_error
                    or self.last_disposition
                )
                raise RuntimeError(
                    "Contact Work session is not active; "
                    f"last terminal state: {detail}"
                )
            sequence = int(sequence)
            if sequence == session.active_sequence and self.endpoint is not None:
                return self._move_result(self.endpoint, "ALREADY_ACCEPTED")
            if sequence != session.active_sequence + 1:
                raise ValueError("Contact Work sequence must advance by exactly one")
            if sequence >= len(session.plan["steps"]):
                raise ValueError("Contact Work sequence is outside the signed plan")
            if time.time_ns() // 1000 >= session.authorization_expires_at_us:
                raise AuthorizationError("Contact Work authorization expired")
            self._assert_assembly_unchanged()
            state = self._fresh_state()
            q_measured = _six(state.get("positions_rad", [])[:6], "measured positions")
            q_control_reference = (
                self.endpoint.q_command.copy()
                if self.endpoint is not None
                else q_measured.copy()
            )
            step = copy.deepcopy(session.plan["steps"][sequence])
            if self.kinematics is None:
                raise RuntimeError("contact kinematics are unavailable")
            name_to_index = {
                name: index for index, name in enumerate(self.kinematics.joint_names)
            }
            locked_indices = tuple(
                sorted(name_to_index[str(name)] for name in step["locked_joint_names"])
            )
            previous_lock_positions = dict(self.lock_positions)
            for index in locked_indices:
                self.lock_positions.setdefault(index, float(q_measured[index]))
            active_locks = {
                index: self.lock_positions[index] for index in locked_indices
            }
            if self.basic_velocity_limits_rad_s is None:
                raise RuntimeError("Basic velocity limits are unavailable")
            velocity_limits = self.basic_velocity_limits_rad_s.copy()
            motion_type = str(step["motion_type"])
            resolved_target = self._resolve_target(q_measured, step["target"])
            segment = None
            if motion_type == "CARTESIAN_SEGMENT":
                segment, ik = self._build_cartesian_segment(
                    q_control_reference,
                    resolved_target,
                    active_locks,
                    velocity_limits,
                )
                velocity_limited_transition_time_s = float(
                    segment.time_waypoints_s[-1]
                )
                q_command = q_control_reference.copy()
            else:
                ik = self._solve_pose(
                    q_control_reference,
                    resolved_target,
                    active_locks,
                )
                velocity_limited_transition_time_s = float(
                    np.max(
                        np.abs(ik.q_goal - q_control_reference)
                        / velocity_limits
                    )
                )
                q_command = ik.q_goal.copy()
            endpoint = ActiveEndpoint(
                sequence,
                step,
                ik.q_goal.copy(),
                locked_indices,
                time.time_ns() // 1000,
                ik.position_residual_m,
                ik.orientation_residual_rad,
                velocity_limits,
                velocity_limited_transition_time_s,
                motion_type,
                q_command,
                resolved_target,
                segment,
            )
            previous_sequence = session.active_sequence
            try:
                self._send_endpoint(state, endpoint)
            except Exception:
                self.lock_positions = previous_lock_positions
                raise
            try:
                if self.position_effort_guard_active:
                    guard = self.basic.lease_snapshot()
                else:
                    guard = self.basic.set_required_command_mode(
                        "POSITION_EFFORT_LIMITED"
                    )
                self.position_effort_guard_active = bool(
                    guard is not None
                    and guard.required_command_mode
                    == "POSITION_EFFORT_LIMITED"
                )
                if not self.position_effort_guard_active:
                    raise RuntimeError(
                        "Basic did not retain the Contact POSITION_EFFORT_LIMITED mode guard"
                    )
            except Exception:
                self.lock_positions = previous_lock_positions
                self._relax(
                    "Contact endpoint could not establish its POSITION_EFFORT_LIMITED mode guard",
                    "MODE_GUARD_FAILED_RELAXED",
                )
                raise
            self.endpoint = endpoint
            session.active_sequence = sequence
            session.deadline_monotonic = (
                time.monotonic()
                + velocity_limited_transition_time_s
                + float(step["next_command_timeout_s"])
            )
            self.control_state = (
                "STREAMING_CARTESIAN_SEGMENT"
                if segment is not None and not segment.complete
                else "HOLDING_POSITION_EFFORT_ENDPOINT"
            )
            self.last_disposition = (
                "ACCEPTED" if previous_sequence < 0 else "SUPERSEDED"
            )
            self.last_error = None
            return self._move_result(endpoint, self.last_disposition)

    def _resolve_target(
        self,
        q_measured: np.ndarray,
        signed_target: dict[str, Any],
    ) -> dict[str, Any]:
        if self.kinematics is None:
            raise RuntimeError("contact kinematics are unavailable")
        resolved = copy.deepcopy(signed_target)
        mode = str(resolved.pop("position_mode"))
        position = np.asarray(resolved["position_m"], dtype=float)
        if mode == "RELATIVE_TO_MEASURED_EFFECTOR_ROOT_AXES":
            measured_position = self.kinematics.evaluate(
                q_measured
            ).controlled_transform[:3, 3]
            position = measured_position + position
        elif mode != "ABSOLUTE_ROOT":
            raise ValueError("target position_mode is unsupported")
        resolved["position_m"] = position.tolist()
        return resolved

    def _solve_pose(
        self,
        q_seed: np.ndarray,
        target: dict[str, Any],
        locked_positions: dict[int, float],
    ) -> Any:
        if self.kinematics is None:
            raise RuntimeError("contact kinematics are unavailable")
        return self.kinematics.solve_pose(
            q_seed,
            target,
            locked_positions,
            maximum_iterations=int(self.config["ik"]["maximum_iterations"]),
            damping=float(self.config["ik"]["damping"]),
            maximum_step_rad=float(self.config["ik"]["maximum_iteration_step_rad"]),
            joint_margin_rad=float(self.config["ik"]["joint_margin_rad"]),
            orientation_weight_m_per_rad=float(
                self.config["ik"]["orientation_weight_m_per_rad"]
            ),
        )

    def _build_cartesian_segment(
        self,
        q_start: np.ndarray,
        goal_target: dict[str, Any],
        locked_positions: dict[int, float],
        velocity_limits_rad_s: np.ndarray,
    ) -> tuple[SegmentTrajectory, Any]:
        if self.kinematics is None or self.basic_control_rate_hz is None:
            raise RuntimeError("contact trajectory dependencies are unavailable")
        start_transform = self.kinematics.evaluate(q_start).controlled_transform
        goal_transform = self.kinematics.target_transform(goal_target)
        start_position = start_transform[:3, 3]
        goal_position = goal_transform[:3, 3]
        distance = float(np.linalg.norm(goal_position - start_position))
        # Defaults keep an existing preserved local configuration compatible
        # when this trajectory section is introduced by a provider upgrade.
        trajectory_config = self.config.get("trajectory", {})
        spacing = float(
            trajectory_config.get("maximum_cartesian_waypoint_spacing_m", 0.002)
        )
        maximum_waypoints = int(
            trajectory_config.get("maximum_waypoints_per_segment", 1000)
        )
        maximum_cartesian_speed_m_s = float(
            trajectory_config.get("maximum_cartesian_speed_m_s", 0.1)
        )
        if not math.isfinite(spacing) or spacing <= 0.0:
            raise RuntimeError("Contact Cartesian waypoint spacing must be positive")
        if maximum_waypoints <= 0:
            raise RuntimeError("Contact Cartesian waypoint budget must be positive")
        if (
            not math.isfinite(maximum_cartesian_speed_m_s)
            or maximum_cartesian_speed_m_s <= 0.0
        ):
            raise RuntimeError("Contact Cartesian speed limit must be positive")
        waypoint_count = max(1, int(math.ceil(distance / spacing)))
        if waypoint_count > maximum_waypoints:
            raise ValueError(
                "Cartesian segment exceeds the configured waypoint budget"
            )
        q_waypoints = [q_start.copy()]
        durations_s = [0.0]
        maximum_position_residual = 0.0
        maximum_orientation_residual = 0.0
        last_ik = None
        for waypoint_index in range(1, waypoint_count + 1):
            self._renew_basic_lease_if_due()
            alpha = waypoint_index / waypoint_count
            target = copy.deepcopy(goal_target)
            target["position_m"] = (
                start_position + alpha * (goal_position - start_position)
            ).tolist()
            last_ik = self._solve_pose(
                q_waypoints[-1],
                target,
                locked_positions,
            )
            q_next = last_ik.q_goal.copy()
            q_delta = np.abs(q_next - q_waypoints[-1])
            durations_s.append(float(np.max(q_delta / velocity_limits_rad_s)))
            q_waypoints.append(q_next)
            maximum_position_residual = max(
                maximum_position_residual,
                float(last_ik.position_residual_m),
            )
            maximum_orientation_residual = max(
                maximum_orientation_residual,
                float(last_ik.orientation_residual_rad),
            )
        if last_ik is None:
            raise AssertionError("Cartesian segment must contain a final IK waypoint")
        time_waypoints = np.cumsum(np.asarray(durations_s, dtype=float))
        control_period_s = 1.0 / self.basic_control_rate_hz
        total_duration_s = float(time_waypoints[-1])
        cartesian_speed_duration_s = distance / maximum_cartesian_speed_m_s
        required_duration_s = max(control_period_s, cartesian_speed_duration_s)
        if total_duration_s < required_duration_s:
            if total_duration_s > 1e-12:
                time_waypoints *= required_duration_s / total_duration_s
            else:
                time_waypoints = np.linspace(
                    0.0,
                    required_duration_s,
                    len(q_waypoints),
                    dtype=float,
                )
        return (
            SegmentTrajectory(
                np.asarray(q_waypoints, dtype=float),
                time_waypoints,
                time.monotonic(),
                distance,
                maximum_position_residual,
                maximum_orientation_residual,
                start_position.copy(),
                goal_position.copy(),
                goal_transform[:3, :3].copy(),
            ),
            last_ik,
        )

    def _renew_basic_lease_if_due(
        self,
        now: float | None = None,
    ) -> None:
        current = time.monotonic() if now is None else float(now)
        if current < self.next_basic_lease_renewal_monotonic:
            return
        self.basic.renew(int(self.config["basic"]["lease_duration_ms"]))
        self.next_basic_lease_renewal_monotonic = (
            time.monotonic()
            + float(self.config["basic"]["lease_renewal_interval_ms"])
            / 1000.0
        )

    @staticmethod
    def _advance_cartesian_segment(
        endpoint: ActiveEndpoint,
        now: float,
    ) -> None:
        segment = endpoint.segment
        if segment is None:
            return
        duration = float(segment.time_waypoints_s[-1])
        elapsed = max(0.0, now - segment.started_monotonic)
        if elapsed >= duration:
            endpoint.q_command = segment.q_waypoints[-1].copy()
            segment.progress = 1.0
            segment.complete = True
        else:
            upper = int(
                np.searchsorted(segment.time_waypoints_s, elapsed, side="right")
            )
            upper = min(max(upper, 1), len(segment.time_waypoints_s) - 1)
            lower = upper - 1
            start_time = float(segment.time_waypoints_s[lower])
            end_time = float(segment.time_waypoints_s[upper])
            alpha = (
                1.0
                if end_time <= start_time
                else (elapsed - start_time) / (end_time - start_time)
            )
            endpoint.q_command = (
                segment.q_waypoints[lower]
                + float(np.clip(alpha, 0.0, 1.0))
                * (segment.q_waypoints[upper] - segment.q_waypoints[lower])
            )
            segment.progress = min(1.0, elapsed / duration)
        segment.command_updates_sent += 1

    def _move_result(
        self, endpoint: ActiveEndpoint, disposition: str
    ) -> dict[str, Any]:
        return {
            "session_id": self.session.session_id if self.session else None,
            "sequence": endpoint.sequence,
            "disposition": disposition,
            "position_residual_m": endpoint.position_residual_m,
            "orientation_residual_rad": endpoint.orientation_residual_rad,
            "joint_velocity_limits_rad_s": endpoint.velocity_limits_rad_s.tolist(),
            "velocity_limited_transition_time_s": (
                endpoint.velocity_limited_transition_time_s
            ),
            "next_command_watchdog_after_transition_s": float(
                endpoint.step["next_command_timeout_s"]
            ),
            "motion_type": endpoint.motion_type,
            "signed_position_mode": endpoint.step["target"]["position_mode"],
            "signed_position_m": endpoint.step["target"]["position_m"],
            "resolved_target_position_m": endpoint.resolved_target["position_m"],
            "cartesian_segment": (
                {
                    "stream_rate_hz": self.basic_control_rate_hz,
                    "cartesian_distance_m": endpoint.segment.cartesian_distance_m,
                    "ik_waypoint_count": len(endpoint.segment.q_waypoints) - 1,
                    "target_waypoint_spacing_m": (
                        endpoint.segment.cartesian_distance_m
                        / (len(endpoint.segment.q_waypoints) - 1)
                    ),
                    "maximum_position_residual_m": (
                        endpoint.segment.maximum_position_residual_m
                    ),
                    "maximum_orientation_residual_rad": (
                        endpoint.segment.maximum_orientation_residual_rad
                    ),
                    "tracking_observation": self._segment_tracking_observation(
                        endpoint.segment
                    ),
                }
                if endpoint.segment is not None
                else None
            ),
            "target_joint_positions_rad": endpoint.q_goal.tolist(),
            "locked_joint_names": [
                self.kinematics.joint_names[index]
                for index in endpoint.locked_indices
            ] if self.kinematics else [],
            "cartesian_arrival_required": False,
        }

    @staticmethod
    def _segment_tracking_observation(
        segment: SegmentTrajectory,
    ) -> dict[str, Any]:
        return {
            "semantics": (
                "KINEMATIC_FK_FROM_BASIC_MEASURED_JOINTS;"
                "PATH_TRACKING_DIAGNOSTIC_NOT_TASK_SUCCESS"
            ),
            "measured_samples": segment.measured_samples,
            "maximum_commanded_cross_track_error_m": (
                segment.maximum_commanded_cross_track_error_m
            ),
            "maximum_commanded_orientation_error_rad": (
                segment.maximum_commanded_orientation_error_rad
            ),
            "maximum_measured_cross_track_error_m": (
                segment.maximum_measured_cross_track_error_m
            ),
            "maximum_measured_orientation_error_rad": (
                segment.maximum_measured_orientation_error_rad
            ),
            "maximum_measured_joint_tracking_error_rad": (
                segment.maximum_measured_joint_tracking_error_rad
            ),
            "last_measured_along_track_fraction": (
                segment.last_measured_along_track_fraction
            ),
        }

    def _accept_fresh_state(self, state: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            self.basic_state = copy.deepcopy(state)
            self.last_state_success_monotonic = time.monotonic()
        try:
            age = float(state.get("feedback_age_ms", math.inf))
        except (TypeError, ValueError):
            age = math.inf
        maximum = float(self.config["basic"]["maximum_feedback_age_ms"])
        if not math.isfinite(age) or age > maximum:
            with self.lock:
                self.ready = False
            raise RuntimeError(f"Basic feedback is stale ({age} ms)")
        if not bool(state.get("ready", False)):
            with self.lock:
                self.ready = False
            raise RuntimeError("Basic is not ready for contact control")
        try:
            _six(state.get("positions_rad", [])[:6], "measured positions")
        except ValueError:
            with self.lock:
                self.ready = False
            raise
        with self.lock:
            self.ready = True
        return state

    def _fresh_state(self) -> dict[str, Any]:
        return self._accept_fresh_state(self.basic.state())

    def _cached_fresh_state(self) -> dict[str, Any]:
        with self.lock:
            state = copy.deepcopy(self.basic_state)
            received = self.last_state_success_monotonic
        try:
            reported_age_ms = float(
                state.get("feedback_age_ms", math.inf)
            )
        except (TypeError, ValueError):
            reported_age_ms = math.inf
        elapsed_ms = (
            max(0.0, time.monotonic() - received) * 1000.0
            if received > 0.0
            else math.inf
        )
        effective_age_ms = reported_age_ms + elapsed_ms
        maximum = float(self.config["basic"]["maximum_feedback_age_ms"])
        if not math.isfinite(effective_age_ms) or effective_age_ms > maximum:
            with self.lock:
                self.ready = False
            raise RuntimeError(
                f"Basic feedback cache is stale ({effective_age_ms} ms)"
            )
        if not bool(state.get("ready", False)):
            with self.lock:
                self.ready = False
            raise RuntimeError("Basic is not ready for contact control")
        _six(state.get("positions_rad", [])[:6], "measured positions")
        return state

    def _state_loop(self) -> None:
        if self.basic_control_rate_hz is None:
            return
        period = 1.0 / self.basic_control_rate_hz
        next_tick = time.monotonic()
        while not self.stop_event.is_set():
            now = time.monotonic()
            if now < next_tick:
                self.stop_event.wait(next_tick - now)
                continue
            try:
                self._fresh_state()
                with self.lock:
                    self.health = "HEALTHY"
                    self.last_error = None
            except Exception as exc:
                with self.lock:
                    self.ready = False
                    self.health = "DEGRADED"
                    self.last_error = f"Basic state refresh failed: {exc}"
            next_tick += period
            finished = time.monotonic()
            if finished > next_tick:
                missed = int((finished - next_tick) // period) + 1
                next_tick += missed * period

    def _send_endpoint(
        self, state: dict[str, Any], endpoint: ActiveEndpoint
    ) -> None:
        if self.kinematics is None:
            raise RuntimeError("contact kinematics are unavailable")
        q_measured = _six(state.get("positions_rad", [])[:6], "measured positions")
        if endpoint.segment is not None:
            self._record_segment_tracking(endpoint, q_measured)
        gravity = np.abs(
            _six(
                state.get("gravity_compensation", {}).get("total_nm", [])[:6],
                "gravity compensation",
            )
        )
        wrench = endpoint.step["wrench"]
        joint_wrench = np.abs(
            self.kinematics.joint_wrench(
                q_measured,
                wrench["force_n"],
                wrench["torque_nm"],
                str(wrench["frame_id"]),
                str(self.acting_frame_id),
            )
        )
        tmax = np.asarray(
            [
                float(joint["motor_limits"]["configured_tmax_nm"])
                for joint in self.basic_model["joints"][:6]
            ],
            dtype=float,
        )
        if self.basic_torque_limits_nm is None:
            raise RuntimeError("Basic torque limits are unavailable")
        caps_nm = self.basic_torque_limits_nm.copy()
        minimum_ratio = float(self.config["wrench"]["minimum_additional_ratio"])
        raw_nm = gravity + np.maximum(joint_wrench, minimum_ratio * tmax)
        limits_nm = np.minimum(raw_nm, caps_nm)
        for index in endpoint.locked_indices:
            limits_nm[index] = float(caps_nm[index])
        saturated = [
            int(index) for index in np.flatnonzero(raw_nm > caps_nm + 1e-12)
        ]
        velocities = endpoint.velocity_limits_rad_s
        commands = []
        for index in range(6):
            commands.append(
                {
                    "joint_index": index,
                    "mode": "POSITION_EFFORT_LIMITED",
                    "values": {
                        "position_rad": float(endpoint.q_command[index]),
                        "velocity_limit_rad_s": float(velocities[index]),
                        "torque_limit_nm": float(limits_nm[index]),
                    },
                }
            )
        self.basic.command(commands, int(self.config["basic"]["command_timeout_ms"]))
        with self.lock:
            self.torque_limits_nm = limits_nm.copy()
            self.gravity_budget_nm = gravity.copy()
            self.wrench_budget_nm = joint_wrench.copy()
            self.saturated_joint_indices = saturated
            self.last_command_at_us = time.time_ns() // 1000

    def _record_segment_tracking(
        self,
        endpoint: ActiveEndpoint,
        q_measured: np.ndarray,
    ) -> None:
        if self.kinematics is None or endpoint.segment is None:
            return
        segment = endpoint.segment
        commanded = self.kinematics.evaluate(endpoint.q_command).controlled_transform
        measured = self.kinematics.evaluate(q_measured).controlled_transform
        line = segment.goal_position_m - segment.start_position_m
        line_length_squared = float(np.dot(line, line))

        def path_sample(pose: np.ndarray) -> tuple[float, float, float]:
            if line_length_squared <= 1e-18:
                along = 0.0
                closest = segment.start_position_m
            else:
                along = float(
                    np.clip(
                        np.dot(pose[:3, 3] - segment.start_position_m, line)
                        / line_length_squared,
                        0.0,
                        1.0,
                    )
                )
                closest = segment.start_position_m + along * line
            cross_track = float(np.linalg.norm(pose[:3, 3] - closest))
            orientation_error = float(
                np.linalg.norm(
                    rotation_vector(
                        segment.goal_rotation @ pose[:3, :3].T
                    )
                )
            )
            return along, cross_track, orientation_error

        _, commanded_cross_track, commanded_orientation_error = path_sample(
            commanded
        )
        measured_along, measured_cross_track, measured_orientation_error = (
            path_sample(measured)
        )
        segment.measured_samples += 1
        segment.maximum_commanded_cross_track_error_m = max(
            segment.maximum_commanded_cross_track_error_m,
            commanded_cross_track,
        )
        segment.maximum_commanded_orientation_error_rad = max(
            segment.maximum_commanded_orientation_error_rad,
            commanded_orientation_error,
        )
        segment.maximum_measured_cross_track_error_m = max(
            segment.maximum_measured_cross_track_error_m,
            measured_cross_track,
        )
        segment.maximum_measured_orientation_error_rad = max(
            segment.maximum_measured_orientation_error_rad,
            measured_orientation_error,
        )
        segment.maximum_measured_joint_tracking_error_rad = max(
            segment.maximum_measured_joint_tracking_error_rad,
            float(np.max(np.abs(q_measured - endpoint.q_command))),
        )
        segment.last_measured_along_track_fraction = measured_along

    def relax(self, session_id: str, reason: str = "Contact Skill completed") -> dict[str, Any]:
        with self.operation_lock:
            if self.session is None:
                return {"disposition": "ALREADY_RELAXED", "float_confirmed": self.float_confirmed}
            if self.session.session_id != str(session_id):
                raise RuntimeError("RELAX does not match the active Contact Work session")
            confirmed = self._relax(reason, "EXPLICITLY_RELAXED")
            return {"disposition": "EXPLICITLY_RELAXED", "float_confirmed": confirmed}

    def _relax(self, reason: str, disposition: str) -> bool:
        had_lease = self.basic.lease_snapshot() is not None
        had_active_control = self.session is not None or self.endpoint is not None
        had_position_effort_guard = self.position_effort_guard_active
        with self.lock:
            self.session = None
            self.endpoint = None
            self.lock_positions = {}
            self.next_basic_lease_renewal_monotonic = 0.0
            self.control_state = "RELAXING_TO_GRAVITY_FLOAT" if had_lease else "WARM_READY"
            self.last_disposition = disposition
            self.last_relax_reason = str(reason)
            self.torque_limits_nm = None
            self.gravity_budget_nm = None
            self.wrench_budget_nm = None
            self.saturated_joint_indices = []
            self.carry_id = None
            self.carry_attachment_revision = None
            self.carry_confirmed = False
            self.position_effort_guard_active = False
        if not had_lease and not had_active_control:
            self.float_confirmed = False
            self.residency = self.requested_residency
            self.control_state = (
                "HOT_READY" if self.residency == "HOT" else "WARM_READY"
            )
            return False
        confirmed = False
        errors: list[str] = []
        try:
            if had_position_effort_guard:
                try:
                    self.basic.set_required_command_mode(None)
                except Exception as exc:
                    errors.append(f"Basic mode guard clear failed: {exc}")
            try:
                self.basic.float(reason)
            except Exception as exc:
                errors.append(f"gravity-float request failed: {exc}")
            deadline = time.monotonic() + float(
                self.config["basic"]["float_verify_timeout_s"]
            )
            while time.monotonic() < deadline:
                try:
                    state = self.basic.state()
                except Exception as exc:
                    errors.append(f"gravity-float verification failed: {exc}")
                    break
                active_modes = state.get("active_command_modes", [])
                pending = state.get("float_transition_pending_joint_indices", [])
                confirmed = bool(
                    isinstance(active_modes, list)
                    and len(active_modes) >= 6
                    and all(str(mode) == "IMPEDANCE" for mode in active_modes[:6])
                    and not any(isinstance(index, int) and index < 6 for index in pending)
                )
                if confirmed:
                    with self.lock:
                        self.basic_state = copy.deepcopy(state)
                    break
                time.sleep(0.03)
        finally:
            try:
                self.basic.release(reason)
            except Exception as exc:
                errors.append(f"Basic lease release failed: {exc}")
        with self.lock:
            if errors:
                self.last_error = "; ".join(errors)
                self.health = "DEGRADED"
            self.float_confirmed = confirmed
            self.residency = self.requested_residency
            self.control_state = (
                ("HOT_READY" if self.residency == "HOT" else "WARM_READY")
                if confirmed
                else "FLOAT_UNCONFIRMED"
            )
        return confirmed

    def _control_loop(self) -> None:
        if self.basic_control_rate_hz is None:
            raise RuntimeError("Basic control rate is unavailable")
        period = 1.0 / self.basic_control_rate_hz
        next_tick = time.monotonic()
        while not self.stop_event.is_set():
            next_tick += period
            with self.operation_lock:
                session = self.session
                if session is None:
                    if (
                        self.state_thread is None
                        or not self.state_thread.is_alive()
                    ):
                        try:
                            self._fresh_state()
                            self.health = "HEALTHY"
                        except Exception as exc:
                            self.ready = False
                            self.health = "DEGRADED"
                            self.last_error = (
                                f"Basic state refresh failed: {exc}"
                            )
                else:
                    now = time.monotonic()
                    if now >= session.deadline_monotonic:
                        if self.carry_confirmed:
                            self._hold_carrying(
                                "Contact Work command watchdog expired",
                                "WATCHDOG_CARRY_HOLD",
                            )
                        else:
                            self._relax(
                                "Contact Work command watchdog expired",
                                "WATCHDOG_RELAXED",
                            )
                    elif time.time_ns() // 1000 >= session.authorization_expires_at_us:
                        if self.carry_confirmed:
                            self._hold_carrying(
                                "Contact Work authorization expired",
                                "AUTHORIZATION_EXPIRED_CARRY_HOLD",
                            )
                        else:
                            self._relax(
                                "Contact Work authorization expired",
                                "AUTHORIZATION_EXPIRED_RELAXED",
                            )
                    else:
                        try:
                            self._renew_basic_lease_if_due(now)
                            if self.endpoint is not None:
                                state = (
                                    self._cached_fresh_state()
                                    if self.state_thread is not None
                                    and self.state_thread.is_alive()
                                    else self._fresh_state()
                                )
                                self._advance_cartesian_segment(self.endpoint, now)
                                self._send_endpoint(state, self.endpoint)
                                if (
                                    self.endpoint.segment is not None
                                    and self.endpoint.segment.complete
                                ):
                                    self.control_state = (
                                        "HOLDING_POSITION_EFFORT_ENDPOINT"
                                    )
                        except Exception as exc:
                            self.last_error = str(exc)
                            self.last_control_fault = str(exc)
                            self.health = "DEGRADED"
                            self._relax(
                                f"Contact control fault: {exc}",
                                "FAULT_RELAXED",
                            )
            delay = next_tick - time.monotonic()
            if delay > 0.0:
                self.stop_event.wait(delay)
            elif delay < -period:
                next_tick = time.monotonic()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            state = copy.deepcopy(self.basic_state)
            session = self.session
            endpoint = self.endpoint
            positions = list(state.get("positions_rad", [])[:6])
            velocities = list(state.get("velocities_rad_s", [])[:6])
            torques = list(state.get("torques_nm", [])[:6])
            measured_acting_frame_pose = None
            if (
                self.kinematics is not None
                and len(positions) == 6
                and all(math.isfinite(float(value)) for value in positions)
            ):
                measured_transform = self.kinematics.evaluate(
                    positions
                ).controlled_transform
                measured_acting_frame_pose = {
                    "frame_id": self.kinematics.root_frame_id,
                    "acting_frame_id": self.acting_frame_id,
                    "position_m": measured_transform[:3, 3].tolist(),
                    "orientation_xyzw": matrix_quaternion(
                        measured_transform[:3, :3]
                    ),
                    "observed_at_us": state.get("observed_at_us"),
                }
            deadline_at_us = None
            if session is not None and math.isfinite(session.deadline_monotonic):
                remaining = max(0.0, session.deadline_monotonic - time.monotonic())
                deadline_at_us = time.time_ns() // 1000 + int(remaining * 1_000_000)
            lease = self.basic.lease_snapshot()
            return {
                "schema": "midbrain.contact_work_state",
                "schema_version": 1,
                "provider_id": self.provider_id,
                "residency": self.residency,
                "control_state": self.control_state,
                "health": self.health,
                "ready": bool(
                    self.ready
                    and not self.motion_inhibited
                    and self.residency == "HOT"
                ),
                "joint_state_valid": bool(self.ready),
                "root_frame_id": self.kinematics.root_frame_id if self.kinematics else None,
                "acting_frame_id": self.acting_frame_id,
                "positions_rad": positions,
                "velocities_rad_s": velocities,
                "torques_nm": torques,
                "measured_acting_frame_pose": measured_acting_frame_pose,
                "joint_observed_at_us": state.get("observed_at_us"),
                "joint_timestamp_uncertainty_us": state.get("timestamp_uncertainty_us"),
                "temperatures_c": list(state.get("temperatures_c", [])[:6]),
                "feedback_age_ms": state.get("feedback_age_ms"),
                "assembly_fingerprint": self.assembly_fingerprint,
                "mounted_effector_revision": self.mounted_effector_revision,
                "arm_resource_id": self.arm_resource_id,
                "carry_id": self.carry_id,
                "carry_attachment_revision": self.carry_attachment_revision,
                "carry_confirmed": self.carry_confirmed,
                "position_effort_guard_active": self.position_effort_guard_active,
                "carry": (
                    {
                        "carry_id": self.carry_id,
                        "attachment_revision": self.carry_attachment_revision,
                        "confirmed": self.carry_confirmed,
                    }
                    if self.carry_id is not None
                    else None
                ),
                "session_id": session.session_id if session else None,
                "skill_id": session.plan["skill_id"] if session else None,
                "active_sequence": endpoint.sequence if endpoint else None,
                "motion_type": endpoint.motion_type if endpoint else None,
                "signed_position_mode": (
                    endpoint.step["target"]["position_mode"] if endpoint else None
                ),
                "signed_position_m": (
                    endpoint.step["target"]["position_m"] if endpoint else None
                ),
                "resolved_target_position_m": (
                    endpoint.resolved_target["position_m"] if endpoint else None
                ),
                "locked_joint_names": [
                    self.kinematics.joint_names[index]
                    for index in (endpoint.locked_indices if endpoint else ())
                ] if self.kinematics else [],
                "target_joint_positions_rad": endpoint.q_goal.tolist() if endpoint else None,
                "commanded_joint_positions_rad": (
                    endpoint.q_command.tolist() if endpoint else None
                ),
                "basic_control_rate_hz": self.basic_control_rate_hz,
                "cartesian_segment": (
                    {
                        "stream_rate_hz": self.basic_control_rate_hz,
                        "cartesian_distance_m": endpoint.segment.cartesian_distance_m,
                        "duration_s": float(endpoint.segment.time_waypoints_s[-1]),
                        "ik_waypoint_count": len(endpoint.segment.q_waypoints) - 1,
                        "target_waypoint_spacing_m": (
                            endpoint.segment.cartesian_distance_m
                            / (len(endpoint.segment.q_waypoints) - 1)
                        ),
                        "command_updates_sent": endpoint.segment.command_updates_sent,
                        "progress": endpoint.segment.progress,
                        "complete": endpoint.segment.complete,
                        "maximum_position_residual_m": (
                            endpoint.segment.maximum_position_residual_m
                        ),
                        "maximum_orientation_residual_rad": (
                            endpoint.segment.maximum_orientation_residual_rad
                        ),
                        "tracking_observation": (
                            self._segment_tracking_observation(endpoint.segment)
                        ),
                    }
                    if endpoint is not None and endpoint.segment is not None
                    else None
                ),
                "velocity_limited_transition_time_s": (
                    endpoint.velocity_limited_transition_time_s
                    if endpoint
                    else None
                ),
                "joint_velocity_limits_rad_s": (
                    endpoint.velocity_limits_rad_s.tolist()
                    if endpoint
                    else (
                        self.basic_velocity_limits_rad_s.tolist()
                        if self.basic_velocity_limits_rad_s is not None
                        else None
                    )
                ),
                "torque_limits_nm": self.torque_limits_nm.tolist() if self.torque_limits_nm is not None else None,
                "gravity_budget_nm": self.gravity_budget_nm.tolist() if self.gravity_budget_nm is not None else None,
                "wrench_budget_nm": self.wrench_budget_nm.tolist() if self.wrench_budget_nm is not None else None,
                "saturated_joint_indices": list(self.saturated_joint_indices),
                "deadline_at_us": deadline_at_us,
                "last_disposition": self.last_disposition,
                "last_error": self.last_error,
                "last_control_fault": self.last_control_fault,
                "last_relax_reason": self.last_relax_reason,
                "float_confirmed": self.float_confirmed,
                "motion_inhibited": self.motion_inhibited,
                "manager_authority_lineage": (
                    copy.deepcopy(session.plan.get("manager_authority"))
                    if session
                    else None
                ),
                "basic_lease": None if lease is None else {
                    "lease_id": lease.lease_id,
                    "fencing_generation": lease.fencing_generation,
                    "resource_id": lease.resource_id,
                    "required_command_mode": lease.required_command_mode,
                },
                "capability_readiness": {
                    "robot_arm.motion.contact.position_effort_limited.v1": bool(
                        self.ready
                        and not self.motion_inhibited
                        and self.residency == "HOT"
                    )
                },
                "completion_semantics": "COMMAND_DISPOSITION_ONLY_NO_CARTESIAN_SUCCESS_CLAIM",
            }
