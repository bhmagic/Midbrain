from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib import error, request
import base64
import hashlib
import hmac
import json
import math
import os
import secrets
import time
import uuid


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def failed_grip_result(
    *,
    target_position_rad: float,
    failure: BaseException,
    cleanup: dict[str, Any],
) -> dict[str, Any]:
    target_degrees = math.degrees(float(target_position_rad))
    cleanup_errors = [str(value) for value in cleanup.get("errors", [])]
    cleanup_complete = not cleanup_errors
    if cleanup_complete:
        disposition = (
            "The gripper was opened and floated, Contact was relaxed, and no "
            "carry was created."
        )
    else:
        disposition = "Cleanup was incomplete: " + "; ".join(cleanup_errors)
    return {
        "status": (
            "FAILED_TO_GRIP"
            if cleanup_complete
            else "FAILED_TO_GRIP_CLEANUP_INCOMPLETE"
        ),
        "workflow_complete": True,
        "physical_motion_requested": True,
        "task_success_assessed": True,
        "task_success": False,
        "grip_confirmed": False,
        "cleanup_complete": cleanup_complete,
        "all_joints_position_effort_limited": False,
        "message": (
            "Failed to grip: no stable contact was confirmed while closing "
            f"toward the {target_degrees:.0f} degree endpoint. {disposition}"
        ),
        "failure_detail": str(failure),
        "cleanup": cleanup,
    }


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _sign(payload: dict[str, Any], secret_env: str) -> str:
    secret = os.getenv(secret_env, "")
    if len(secret.encode("utf-8")) < 32:
        raise RuntimeError(f"signing secret {secret_env} is not configured")
    segment = _base64url(canonical_bytes(payload))
    signature = hmac.new(
        secret.encode("utf-8"), segment.encode("ascii"), hashlib.sha256
    ).digest()
    return f"{segment}.{_base64url(signature)}"


class HttpStatusError(RuntimeError):
    def __init__(self, status_code: int, payload: dict[str, Any], body: str):
        super().__init__(
            f"Provider API HTTP {status_code}: "
            f"{payload.get('error') or payload.get('detail') or body}"
        )
        self.status_code = int(status_code)
        self.payload = dict(payload)


class JsonClient:
    def __init__(self, timeout_s: float = 3.0):
        self.timeout_s = float(timeout_s)

    def get(self, url: str) -> dict[str, Any]:
        return self._request("GET", url, None, {})

    def post(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return self._request("POST", url, payload, headers or {})

    def _request(
        self,
        method: str,
        url: str,
        payload: dict[str, Any] | None,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        data = None if payload is None else canonical_bytes(payload)
        request_headers = {"Accept": "application/json", **headers}
        if data is not None:
            request_headers["Content-Type"] = "application/json"
        operation = request.Request(
            url, data=data, method=method, headers=request_headers
        )
        try:
            with request.urlopen(operation, timeout=self.timeout_s) as response:
                raw = response.read()
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                payload = {"error": body}
            if not isinstance(payload, dict):
                payload = {"error": body}
            raise HttpStatusError(exc.code, payload, body) from exc
        if not raw:
            return {}
        decoded = json.loads(raw)
        if not isinstance(decoded, dict):
            raise ValueError("Provider API response must be an object")
        return decoded


@dataclass(frozen=True)
class ContactIdentity:
    provider_id: str
    provider_instance_id: str
    provider_boot_id: str
    assembly_fingerprint: str
    mounted_effector_revision: str
    acting_frame_id: str
    root_frame_id: str
    arm_resource_id: str

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> "ContactIdentity":
        names = {
            name: state.get(name)
            for name in (
                "provider_id",
                "provider_instance_id",
                "provider_boot_id",
                "assembly_fingerprint",
                "mounted_effector_revision",
                "acting_frame_id",
                "root_frame_id",
                "arm_resource_id",
            )
        }
        if not bool(state.get("ready")) or any(
            not str(value or "").strip() for value in names.values()
        ):
            raise RuntimeError("Contact Provider identity is incomplete or not ready")
        return cls(**{name: str(value) for name, value in names.items()})


def _vector(value: Any, length: int, name: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise ValueError(f"{name} must contain {length} values")
    result = [float(component) for component in value]
    if not all(math.isfinite(component) for component in result):
        raise ValueError(f"{name} must be finite")
    return result


def contact_step(
    *,
    position_m: list[float],
    orientation_xyzw: list[float],
    force_n: list[float] | None = None,
    torque_nm: list[float] | None = None,
    position_mode: str = "RELATIVE_TO_MEASURED_EFFECTOR_ROOT_AXES",
    delay_after_accept_s: float = 0.25,
    next_command_timeout_s: float = 6.0,
) -> dict[str, Any]:
    delay = float(delay_after_accept_s)
    timeout = float(next_command_timeout_s)
    if delay < 0.0 or timeout <= delay:
        raise ValueError("Contact delay must be non-negative and below its timeout")
    return {
        "position_m": _vector(position_m, 3, "position_m"),
        "orientation_xyzw": _vector(orientation_xyzw, 4, "orientation_xyzw"),
        "force_n": _vector(force_n or [0.0, 0.0, 0.0], 3, "force_n"),
        "torque_nm": _vector(torque_nm or [0.0, 0.0, 0.0], 3, "torque_nm"),
        "position_mode": str(position_mode).upper(),
        "delay_after_accept_s": delay,
        "next_command_timeout_s": timeout,
    }


class ManagerAuthorityLease:
    def __init__(
        self,
        client: JsonClient,
        manager_url: str,
        resource_id: str,
        owner_id: str,
        skill_id: str,
        duration_ms: int = 6000,
        renewal_interval_ms: int = 1000,
    ):
        self.client = client
        self.manager_url = manager_url.rstrip("/")
        self.resource_id = resource_id
        self.owner_id = owner_id
        self.skill_id = skill_id
        self.duration_ms = int(duration_ms)
        self.renewal_interval_ms = int(renewal_interval_ms)
        self.lease_id: str | None = None
        self.next_renewal = 0.0

    def acquire(self) -> dict[str, Any]:
        lease = self.client.post(
            f"{self.manager_url}/v1/control-authority/leases",
            {
                "resource_id": self.resource_id,
                "owner_id": self.owner_id,
                "permissions": ["execute_contact", "relax"],
                "duration_ms": self.duration_ms,
                "renewal_interval_ms": self.renewal_interval_ms,
                "preempt": False,
                "safe_relinquish": "CONTACT_PROVIDER_CARRY_HOLD_OR_VERIFIED_FLOAT",
                "related_skill_id": self.skill_id,
            },
        )
        self.lease_id = str(lease["lease_id"])
        self.next_renewal = (
            time.monotonic() + self.renewal_interval_ms / 1000.0
        )
        return lease

    def service(self) -> None:
        if self.lease_id is None or time.monotonic() < self.next_renewal:
            return
        self.client.post(
            f"{self.manager_url}/v1/control-authority/leases/{self.lease_id}/renew",
            {"owner_id": self.owner_id, "duration_ms": self.duration_ms},
        )
        self.next_renewal = (
            time.monotonic() + self.renewal_interval_ms / 1000.0
        )

    def release(self, reason: str) -> None:
        if self.lease_id is None:
            return
        try:
            self.client.post(
                f"{self.manager_url}/v1/control-authority/leases/{self.lease_id}/release",
                {"owner_id": self.owner_id, "reason": reason},
            )
        finally:
            self.lease_id = None


class ContactCarryRuntime:
    def __init__(
        self,
        provider_url: str,
        manager_url: str,
        *,
        signing_secret_env: str,
        client: JsonClient | None = None,
    ):
        self.provider_url = provider_url.rstrip("/")
        self.manager_url = manager_url.rstrip("/")
        self.signing_secret_env = signing_secret_env
        self.client = client or JsonClient()

    def state(self) -> dict[str, Any]:
        return self.client.get(f"{self.provider_url}/v1/contact/state")

    def _sign_plan(self, plan: dict[str, Any], identity: ContactIdentity) -> str:
        now = time.time_ns() // 1000
        duration_s = min(
            60.0,
            15.0
            + sum(float(step["next_command_timeout_s"]) for step in plan["steps"]),
        )
        return _sign(
            {
                "schema": "midbrain.contact_work_authorization",
                "schema_version": 1,
                "assertion_id": str(uuid.uuid4()),
                "nonce": secrets.token_urlsafe(24),
                "issuer_skill_id": plan["skill_id"],
                "execution_id": plan["execution_id"],
                "audience_provider_id": identity.provider_id,
                "provider_instance_id": identity.provider_instance_id,
                "provider_boot_id": identity.provider_boot_id,
                "assembly_fingerprint": identity.assembly_fingerprint,
                "mounted_effector_revision": identity.mounted_effector_revision,
                "plan_sha256": canonical_sha256(plan),
                "issued_at_us": now,
                "expires_at_us": now + int(duration_s * 1_000_000),
            },
            self.signing_secret_env,
        )

    def _settling_observation(
        self,
        session_id: str,
        sequence: int,
    ) -> dict[str, Any]:
        return self.client.post(
            f"{self.provider_url}/v1/contact/settling",
            {
                "session_id": str(session_id),
                "sequence": int(sequence),
                "maximum_joint_error_rad": 0.04,
                "maximum_joint_velocity_rad_s": 0.05,
            },
        )

    @staticmethod
    def _service_for(
        duration_s: float,
        service: Any,
    ) -> None:
        deadline = time.monotonic() + max(0.0, float(duration_s))
        while time.monotonic() < deadline:
            service()
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    def _wait_for_trajectory_then_dwell(
        self,
        *,
        session_id: str,
        sequence: int,
        transition_time_s: float,
        next_command_timeout_s: float,
        dwell_s: float,
        service: Any,
    ) -> dict[str, Any]:
        transition = max(0.0, float(transition_time_s))
        timeout = float(next_command_timeout_s)
        dwell = max(0.0, float(dwell_s))
        trajectory_budget = timeout - dwell - 0.25
        if trajectory_budget <= 0.0:
            raise RuntimeError(
                "Contact stage has no watchdog budget for trajectory completion"
            )
        deadline = time.monotonic() + transition + trajectory_budget
        last: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            service()
            last = self._settling_observation(session_id, sequence)
            if last.get("trajectory_complete") is True:
                self._service_for(dwell, service)
                return {
                    "trajectory_completion": last,
                    "dwell_after_trajectory_s": dwell,
                }
            time.sleep(0.05)
        raise TimeoutError(
            "Contact trajectory did not complete before its signed stage dwell budget; "
            f"last_settling_observation={json.dumps(last, ensure_ascii=False, default=str)}"
        )

    def wait_for_settled(
        self,
        session_id: str,
        sequence: int,
        *,
        timeout_s: float = 5.0,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + float(timeout_s)
        last: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            last = self._settling_observation(session_id, sequence)
            if last.get("settled") is True:
                return last
            time.sleep(0.05)
        raise TimeoutError(
            "Contact arm endpoint did not settle while POSITION_EFFORT_LIMITED "
            "remained guarded; "
            f"last_settling_observation={json.dumps(last, ensure_ascii=False, default=str)}"
        )

    def execute(
        self,
        *,
        skill_id: str,
        steps: list[dict[str, Any]],
        carry_id: str,
        attachment_revision: str,
        behavior: str,
        confirm_carry: bool = False,
    ) -> dict[str, Any]:
        if not steps:
            raise ValueError("a Contact carry plan requires at least one finite step")
        identity = ContactIdentity.from_state(self.state())
        execution_id = str(uuid.uuid4())
        authority = ManagerAuthorityLease(
            self.client,
            self.manager_url,
            identity.arm_resource_id,
            execution_id,
            skill_id,
        )
        authority_record = authority.acquire()
        plan_steps = []
        session_id = ""
        moves = []
        settlement = None
        try:
            for sequence, step in enumerate(steps):
                plan_steps.append(
                    {
                        "sequence": sequence,
                        "motion_type": "CARTESIAN_SEGMENT",
                        "target": {
                            "frame_id": identity.root_frame_id,
                            "position_mode": step["position_mode"],
                            "position_m": step["position_m"],
                            "orientation_xyzw": step["orientation_xyzw"],
                        },
                        "wrench": {
                            "frame_id": identity.root_frame_id,
                            "force_n": step["force_n"],
                            "torque_nm": step["torque_nm"],
                        },
                        "locked_joint_names": [],
                        "delay_after_accept_s": step["delay_after_accept_s"],
                        "next_command_timeout_s": step["next_command_timeout_s"],
                    }
                )
            plan = {
                "schema": "midbrain.contact_work_plan",
                "schema_version": 2,
                "plan_id": str(uuid.uuid4()),
                "skill_id": skill_id,
                "execution_id": execution_id,
                "provider_id": identity.provider_id,
                "assembly_fingerprint": identity.assembly_fingerprint,
                "acting_frame_id": identity.acting_frame_id,
                "manager_authority": {
                    "resource_id": str(authority_record["resource_id"]),
                    "lease_id": str(authority_record["lease_id"]),
                    "owner_id": str(authority_record["owner_id"]),
                    "fencing_generation": int(authority_record["fencing_generation"]),
                    "permissions": [str(value) for value in authority_record["permissions"]],
                },
                "carry": {
                    "behavior": str(behavior).upper(),
                    "carry_id": str(carry_id),
                    "attachment_revision": str(attachment_revision),
                },
                "steps": plan_steps,
            }
            assertion = self._sign_plan(plan, identity)
            session = self.client.post(
                f"{self.provider_url}/v1/contact/session",
                {"plan": plan},
                {"X-Midbrain-Authorization": assertion},
            )
            session_id = str(session["session_id"])
            for step in plan_steps:
                authority.service()
                result = self.client.post(
                    f"{self.provider_url}/v1/contact/move",
                    {"session_id": session_id, "sequence": step["sequence"]},
                )
                transition = float(result.get("velocity_limited_transition_time_s", 0.0))
                runtime_completion = self._wait_for_trajectory_then_dwell(
                    session_id=session_id,
                    sequence=int(step["sequence"]),
                    transition_time_s=transition,
                    next_command_timeout_s=float(step["next_command_timeout_s"]),
                    dwell_s=float(step["delay_after_accept_s"]),
                    service=authority.service,
                )
                moves.append({**result, "runtime_completion": runtime_completion})
            settlement = self._settling_observation(
                session_id,
                int(plan_steps[-1]["sequence"]),
            )
            if confirm_carry:
                settlement = self.wait_for_settled(
                    session_id,
                    int(plan_steps[-1]["sequence"]),
                )
                self.client.post(
                    f"{self.provider_url}/v1/contact/carry/confirm",
                    {
                        "session_id": session_id,
                        "carry_id": carry_id,
                        "attachment_revision": attachment_revision,
                    },
                )
            return {
                "execution_id": execution_id,
                "session_id": session_id,
                "plan": plan,
                "moves": moves,
                "settlement": settlement,
                "carry_confirmed": confirm_carry,
            }
        finally:
            authority.release(f"{skill_id} finite submission complete")

    def relax(self, session_id: str, reason: str) -> dict[str, Any]:
        return self.client.post(
            f"{self.provider_url}/v1/contact/relax",
            {"session_id": session_id, "reason": reason},
        )

    def confirm_carry(
        self,
        session_id: str,
        carry_id: str,
        attachment_revision: str,
        *,
        settle_timeout_s: float = 5.0,
    ) -> dict[str, Any]:
        state = self.state()
        sequence = state.get("active_sequence")
        if sequence is None:
            raise RuntimeError("Contact has no active endpoint to settle before carry")
        settling = self.wait_for_settled(
            session_id,
            int(sequence),
            timeout_s=settle_timeout_s,
        )
        result = self.client.post(
            f"{self.provider_url}/v1/contact/carry/confirm",
            {
                "session_id": session_id,
                "carry_id": carry_id,
                "attachment_revision": attachment_revision,
            },
        )
        return {**result, "settling": settling}


class ContactStagedRuntime(ContactCarryRuntime):
    """Keep one signed Contact plan alive across attended development stages."""

    def __init__(
        self,
        provider_url: str,
        manager_url: str,
        *,
        signing_secret_env: str,
        client: JsonClient | None = None,
    ):
        super().__init__(
            provider_url,
            manager_url,
            signing_secret_env=signing_secret_env,
            client=client,
        )
        self.authority: ManagerAuthorityLease | None = None
        self.plan: dict[str, Any] | None = None
        self.session_id: str | None = None
        self.next_sequence = 0
        self.carry_id: str | None = None
        self.attachment_revision: str | None = None

    def begin(
        self,
        *,
        skill_id: str,
        steps: list[dict[str, Any]],
        carry_id: str,
        attachment_revision: str,
        behavior: str,
    ) -> dict[str, Any]:
        if self.session_id is not None or self.authority is not None:
            raise RuntimeError("a staged Contact development session is already active")
        if not steps:
            raise ValueError("a staged Contact plan requires at least one step")
        identity = ContactIdentity.from_state(self.state())
        execution_id = str(uuid.uuid4())
        authority = ManagerAuthorityLease(
            self.client,
            self.manager_url,
            identity.arm_resource_id,
            execution_id,
            skill_id,
            duration_ms=60_000,
            renewal_interval_ms=5_000,
        )
        authority_record = authority.acquire()
        plan_steps = []
        try:
            for sequence, step in enumerate(steps):
                plan_steps.append(
                    {
                        "sequence": sequence,
                        "motion_type": "CARTESIAN_SEGMENT",
                        "target": {
                            "frame_id": identity.root_frame_id,
                            "position_mode": step["position_mode"],
                            "position_m": step["position_m"],
                            "orientation_xyzw": step["orientation_xyzw"],
                        },
                        "wrench": {
                            "frame_id": identity.root_frame_id,
                            "force_n": step["force_n"],
                            "torque_nm": step["torque_nm"],
                        },
                        "locked_joint_names": [],
                        "delay_after_accept_s": step["delay_after_accept_s"],
                        "next_command_timeout_s": step["next_command_timeout_s"],
                    }
                )
            plan = {
                "schema": "midbrain.contact_work_plan",
                "schema_version": 2,
                "plan_id": str(uuid.uuid4()),
                "skill_id": skill_id,
                "execution_id": execution_id,
                "provider_id": identity.provider_id,
                "assembly_fingerprint": identity.assembly_fingerprint,
                "acting_frame_id": identity.acting_frame_id,
                "manager_authority": {
                    "resource_id": str(authority_record["resource_id"]),
                    "lease_id": str(authority_record["lease_id"]),
                    "owner_id": str(authority_record["owner_id"]),
                    "fencing_generation": int(
                        authority_record["fencing_generation"]
                    ),
                    "permissions": [
                        str(value) for value in authority_record["permissions"]
                    ],
                },
                "carry": {
                    "behavior": str(behavior).upper(),
                    "carry_id": str(carry_id),
                    "attachment_revision": str(attachment_revision),
                },
                "steps": plan_steps,
            }
            assertion = self._sign_plan(plan, identity)
            response = self.client.post(
                f"{self.provider_url}/v1/contact/session",
                {"plan": plan},
                {"X-Midbrain-Authorization": assertion},
            )
        except Exception:
            authority.release("staged Contact development preparation failed")
            raise
        self.authority = authority
        self.plan = plan
        self.session_id = str(response["session_id"])
        self.next_sequence = 0
        self.carry_id = str(carry_id)
        self.attachment_revision = str(attachment_revision)
        return {
            "execution_id": execution_id,
            "session_id": self.session_id,
            "plan": plan,
        }

    def move(self, sequence: int) -> dict[str, Any]:
        if self.plan is None or self.session_id is None or self.authority is None:
            raise RuntimeError("no staged Contact development session is active")
        requested = int(sequence)
        if requested != self.next_sequence:
            raise RuntimeError(
                f"Contact stage sequence {requested} is unavailable; "
                f"the next sequence is {self.next_sequence}"
            )
        step = self.plan["steps"][requested]
        self.authority.service()
        result = self.client.post(
            f"{self.provider_url}/v1/contact/move",
            {"session_id": self.session_id, "sequence": requested},
        )
        transition = float(result.get("velocity_limited_transition_time_s", 0.0))
        runtime_completion = self._wait_for_trajectory_then_dwell(
            session_id=self.session_id,
            sequence=requested,
            transition_time_s=transition,
            next_command_timeout_s=float(step["next_command_timeout_s"]),
            dwell_s=float(step["delay_after_accept_s"]),
            service=self.authority.service,
        )
        settling = self._settling_observation(self.session_id, requested)
        self.next_sequence += 1
        return {
            "move": {**result, "runtime_completion": runtime_completion},
            "settling": settling,
            "next_command_deadline_at_us": (
                time.time_ns() // 1000
                + int(float(step["next_command_timeout_s"]) * 1_000_000)
            ),
        }

    def confirm_staged_carry(self) -> dict[str, Any]:
        if (
            self.session_id is None
            or self.carry_id is None
            or self.attachment_revision is None
            or self.authority is None
        ):
            raise RuntimeError("no staged Contact carry is ready for confirmation")
        if self.plan is None or self.next_sequence != len(self.plan["steps"]):
            raise RuntimeError("all staged Contact moves must finish before carry confirmation")
        self.authority.service()
        settling = self.wait_for_settled(
            self.session_id,
            self.next_sequence - 1,
            timeout_s=5.0,
        )
        confirmed = self.client.post(
            f"{self.provider_url}/v1/contact/carry/confirm",
            {
                "session_id": self.session_id,
                "carry_id": self.carry_id,
                "attachment_revision": self.attachment_revision,
            },
        )
        return {**confirmed, "settling": settling}

    def settling(self, sequence: int) -> dict[str, Any]:
        if self.session_id is None:
            raise RuntimeError("no staged Contact session is available")
        return self._settling_observation(self.session_id, int(sequence))

    def relax_staged(self, reason: str) -> dict[str, Any]:
        if self.session_id is None:
            raise RuntimeError("no staged Contact session is available to relax")
        try:
            return self.relax(self.session_id, reason)
        finally:
            self.close(reason)

    def close(self, reason: str) -> None:
        authority = self.authority
        self.authority = None
        if authority is not None:
            authority.release(reason)


class GripRuntime:
    def __init__(
        self,
        provider_url: str,
        *,
        signing_secret_env: str,
        client: JsonClient | None = None,
    ):
        self.provider_url = provider_url.rstrip("/")
        self.signing_secret_env = signing_secret_env
        self.client = client or JsonClient()

    def state(self) -> dict[str, Any]:
        return self.client.get(f"{self.provider_url}/v1/grip/state")

    def command(
        self,
        *,
        skill_id: str,
        execution_id: str,
        operation: str,
        **values: Any,
    ) -> dict[str, Any]:
        state = self.state()
        command = {
            "schema": "midbrain.grip_control_command",
            "schema_version": 1,
            "command_id": str(uuid.uuid4()),
            "skill_id": skill_id,
            "execution_id": execution_id,
            "operation": str(operation).upper(),
            **values,
        }
        now = time.time_ns() // 1000
        assertion = _sign(
            {
                "schema": "midbrain.grip_control_authorization",
                "schema_version": 1,
                "assertion_id": str(uuid.uuid4()),
                "nonce": secrets.token_urlsafe(24),
                "issuer_skill_id": skill_id,
                "execution_id": execution_id,
                "audience_provider_id": state["provider_id"],
                "provider_instance_id": state["provider_instance_id"],
                "provider_boot_id": state["provider_boot_id"],
                "assembly_fingerprint": state["assembly_fingerprint"],
                "mounted_effector_revision": state["mounted_effector_revision"],
                "command_sha256": canonical_sha256(command),
                "issued_at_us": now,
                "expires_at_us": now + 30_000_000,
            },
            self.signing_secret_env,
        )
        return self.client.post(
            f"{self.provider_url}/v1/grip/command",
            {"command": command},
            {"X-Midbrain-Authorization": assertion},
        )

    def wait_for(self, predicate, *, timeout_s: float, description: str) -> dict[str, Any]:
        deadline = time.monotonic() + float(timeout_s)
        while True:
            state = self.state()
            if predicate(state):
                return state
            if time.monotonic() >= deadline:
                diagnostic_fields = (
                    "state",
                    "gripper_position_rad",
                    "gripper_velocity_rad_s",
                    "gripper_torque_nm",
                    "target",
                    "functionally_open",
                    "ready_for_approach",
                    "contact_inferred",
                    "contact_stable_samples",
                )
                diagnostics = {
                    field: state[field]
                    for field in diagnostic_fields
                    if field in state
                }
                raise TimeoutError(
                    f"timed out waiting for {description}; "
                    f"last_grip_state={json.dumps(diagnostics, sort_keys=True)}"
                )
            time.sleep(0.05)

    def open_and_float(
        self,
        *,
        skill_id: str,
        execution_id: str,
        position_rad: float,
        velocity_limit_rad_s: float,
        torque_limit_nm: float,
        position_tolerance_rad: float,
        open_timeout_s: float,
        mit_delta_time_s: float,
    ) -> dict[str, Any]:
        """Open an unconfirmed grip, verify release, and leave the gripper floating."""
        requested_position = float(position_rad)
        tolerance = float(position_tolerance_rad)
        if tolerance <= 0.0 or float(open_timeout_s) <= 0.0:
            raise ValueError("release tolerance and timeout must be positive")
        opened = None
        measured = None
        floated = None
        float_state = None
        errors = []
        try:
            opened = self.command(
                skill_id=skill_id,
                execution_id=execution_id,
                operation="SET_POSITION_EFFORT",
                intent="OPEN",
                position_rad=requested_position,
                velocity_limit_rad_s=float(velocity_limit_rad_s),
                torque_limit_nm=float(torque_limit_nm),
            )
            measured = self.wait_for(
                lambda state: state.get("functionally_open") is True
                or (
                    state.get("gripper_position_rad") is not None
                    and abs(
                        float(state["gripper_position_rad"]) - requested_position
                    )
                    <= tolerance
                ),
                timeout_s=float(open_timeout_s),
                description="failed-grip release opening",
            )
        except Exception as exc:
            errors.append(f"open verification failed: {exc}")
        try:
            floated = self.command(
                skill_id=skill_id,
                execution_id=execution_id,
                operation="ENTER_MIT_FLOAT",
                delta_time_s=float(mit_delta_time_s),
            )
            float_state = self.wait_for(
                lambda state: state.get("state") == "MIT_FLOAT",
                timeout_s=max(2.0, float(mit_delta_time_s) * 2.0 + 1.0),
                description="failed-grip MIT float completion",
            )
        except Exception as exc:
            errors.append(f"float transition failed: {exc}")
        if errors:
            raise RuntimeError("; ".join(errors))
        return {
            "open_command": opened,
            "open_state": measured,
            "float_command": floated,
            "float_state": float_state,
        }
