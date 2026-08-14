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


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


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
            raise RuntimeError(f"Contact API HTTP {exc.code}: {body}") from exc
        if not raw:
            return {}
        decoded = json.loads(raw)
        if not isinstance(decoded, dict):
            raise ValueError("Contact API response must be an object")
        return decoded


@dataclass(frozen=True)
class ProviderIdentity:
    provider_id: str
    provider_instance_id: str
    provider_boot_id: str
    assembly_fingerprint: str
    mounted_effector_revision: str
    acting_frame_id: str
    root_frame_id: str
    arm_resource_id: str

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> "ProviderIdentity":
        if not bool(state.get("ready", False)):
            raise RuntimeError("Contact Provider is not ready")
        names = {
            "provider_id": state.get("provider_id"),
            "provider_instance_id": state.get("provider_instance_id"),
            "provider_boot_id": state.get("provider_boot_id"),
            "assembly_fingerprint": state.get("assembly_fingerprint"),
            "mounted_effector_revision": state.get("mounted_effector_revision"),
            "acting_frame_id": state.get("acting_frame_id"),
            "root_frame_id": state.get("root_frame_id"),
            "arm_resource_id": state.get("arm_resource_id"),
        }
        if any(not str(value or "").strip() for value in names.values()):
            missing = [name for name, value in names.items() if not str(value or "").strip()]
            raise RuntimeError(
                "Contact Provider identity is incomplete: " + ", ".join(missing)
            )
        return cls(**{name: str(value) for name, value in names.items()})


@dataclass(frozen=True)
class ContactStep:
    position_m: tuple[float, float, float]
    orientation_xyzw: tuple[float, float, float, float]
    force_n: tuple[float, float, float]
    torque_nm: tuple[float, float, float] = (0.0, 0.0, 0.0)
    motion_type: str = "ONE_SHOT"
    position_mode: str = "ABSOLUTE_ROOT"
    locked_joint_names: tuple[str, ...] = ()
    delay_after_accept_s: float = 0.25
    next_command_timeout_s: float = 6.0
    wrench_in_acting_frame: bool = True

    def payload(
        self,
        sequence: int,
        identity: ProviderIdentity,
    ) -> dict[str, Any]:
        vectors = (
            self.position_m,
            self.orientation_xyzw,
            self.force_n,
            self.torque_nm,
        )
        if not all(math.isfinite(float(value)) for vector in vectors for value in vector):
            raise ValueError("Contact Skill step contains a non-finite value")
        if len(self.position_m) != 3 or len(self.orientation_xyzw) != 4:
            raise ValueError("Contact Skill pose dimensions are invalid")
        if len(self.force_n) != 3 or len(self.torque_nm) != 3:
            raise ValueError("Contact Skill wrench dimensions are invalid")
        if self.delay_after_accept_s < 0.0:
            raise ValueError("Contact Skill delay must be non-negative")
        if self.next_command_timeout_s <= self.delay_after_accept_s:
            raise ValueError("Contact Skill watchdog timeout must exceed its delay")
        motion_type = str(self.motion_type).strip().upper()
        if motion_type not in {"ONE_SHOT", "CARTESIAN_SEGMENT"}:
            raise ValueError("Contact Skill motion_type is unsupported")
        position_mode = str(self.position_mode).strip().upper()
        if position_mode not in {
            "ABSOLUTE_ROOT",
            "RELATIVE_TO_MEASURED_EFFECTOR_ROOT_AXES",
        }:
            raise ValueError("Contact Skill position_mode is unsupported")
        return {
            "sequence": int(sequence),
            "motion_type": motion_type,
            "target": {
                "frame_id": identity.root_frame_id,
                "position_mode": position_mode,
                "position_m": [float(value) for value in self.position_m],
                "orientation_xyzw": [
                    float(value) for value in self.orientation_xyzw
                ],
            },
            "wrench": {
                "frame_id": (
                    identity.acting_frame_id
                    if self.wrench_in_acting_frame
                    else identity.root_frame_id
                ),
                "force_n": [float(value) for value in self.force_n],
                "torque_nm": [float(value) for value in self.torque_nm],
            },
            "locked_joint_names": [str(value) for value in self.locked_joint_names],
            "delay_after_accept_s": float(self.delay_after_accept_s),
            "next_command_timeout_s": float(self.next_command_timeout_s),
        }


class ManagerAuthorityLease:
    def __init__(
        self,
        client: JsonClient,
        manager_url: str,
        resource_id: str,
        owner_id: str,
        skill_id: str,
    ):
        self.client = client
        self.manager_url = manager_url.rstrip("/")
        self.resource_id = resource_id
        self.owner_id = owner_id
        self.skill_id = skill_id
        self.lease_id: str | None = None
        self.next_renewal = 0.0

    def acquire(self) -> dict[str, Any]:
        lease = self.client.post(
            f"{self.manager_url}/v1/control-authority/leases",
            {
                "resource_id": self.resource_id,
                "owner_id": self.owner_id,
                "permissions": ["execute_contact", "relax"],
                "duration_ms": 6000,
                "renewal_interval_ms": 1000,
                "preempt": False,
                "safe_relinquish": "CONTACT_PROVIDER_VERIFIED_GRAVITY_FLOAT",
                "related_skill_id": self.skill_id,
            },
        )
        self.lease_id = str(lease["lease_id"])
        self.next_renewal = time.monotonic() + 1.0
        return lease

    def service(self) -> None:
        if self.lease_id is None or time.monotonic() < self.next_renewal:
            return
        self.client.post(
            f"{self.manager_url}/v1/control-authority/leases/{self.lease_id}/renew",
            {"owner_id": self.owner_id, "duration_ms": 6000},
        )
        self.next_renewal = time.monotonic() + 1.0

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


class ContactWorkRuntime:
    """Runs one finite signed plan and always attempts terminal relaxation."""

    def __init__(
        self,
        provider_url: str = "http://127.0.0.1:8794",
        manager_url: str = "http://127.0.0.1:7001",
        *,
        signing_secret_env: str,
        client: JsonClient | None = None,
        velocity_transition_margin_ratio: float = 1.25,
        velocity_transition_margin_s: float = 0.10,
    ):
        self.provider_url = provider_url.rstrip("/")
        self.manager_url = manager_url.rstrip("/")
        if not str(signing_secret_env).strip():
            raise ValueError("Contact Skill signing_secret_env must be explicit")
        self.signing_secret_env = str(signing_secret_env)
        self.client = client or JsonClient()
        self.velocity_transition_margin_ratio = float(
            velocity_transition_margin_ratio
        )
        self.velocity_transition_margin_s = float(velocity_transition_margin_s)
        if (
            not math.isfinite(self.velocity_transition_margin_ratio)
            or self.velocity_transition_margin_ratio < 1.0
        ):
            raise ValueError("velocity_transition_margin_ratio must be at least 1")
        if (
            not math.isfinite(self.velocity_transition_margin_s)
            or self.velocity_transition_margin_s < 0.0
        ):
            raise ValueError("velocity_transition_margin_s must be non-negative")

    def effective_hold_s(
        self,
        planned_delay_s: float,
        move_result: dict[str, Any],
    ) -> float:
        planned = float(planned_delay_s)
        transition = float(
            move_result.get("velocity_limited_transition_time_s", 0.0)
        )
        if not math.isfinite(planned) or planned < 0.0:
            raise ValueError("planned Contact delay must be finite and non-negative")
        if not math.isfinite(transition) or transition < 0.0:
            raise ValueError(
                "Contact velocity-limited transition time must be finite and non-negative"
            )
        velocity_floor = (
            transition * self.velocity_transition_margin_ratio
            + self.velocity_transition_margin_s
            if transition > 0.0
            else 0.0
        )
        return max(planned, velocity_floor)

    def provider_identity(self) -> ProviderIdentity:
        return ProviderIdentity.from_state(
            self.client.get(f"{self.provider_url}/health")
        )

    def build_plan(
        self,
        skill_id: str,
        steps: list[ContactStep],
        *,
        identity: ProviderIdentity,
        manager_authority: dict[str, Any],
        execution_id: str | None = None,
    ) -> dict[str, Any]:
        if not steps:
            raise ValueError("Contact Skill requires at least one step")
        execution = execution_id or str(uuid.uuid4())
        return {
            "schema": "midbrain.contact_work_plan",
            "schema_version": 1,
            "plan_id": str(uuid.uuid4()),
            "skill_id": str(skill_id),
            "execution_id": execution,
            "provider_id": identity.provider_id,
            "assembly_fingerprint": identity.assembly_fingerprint,
            "acting_frame_id": identity.acting_frame_id,
            "manager_authority": {
                "resource_id": str(manager_authority["resource_id"]),
                "lease_id": str(manager_authority["lease_id"]),
                "owner_id": str(manager_authority["owner_id"]),
                "fencing_generation": int(manager_authority["fencing_generation"]),
                "permissions": [str(value) for value in manager_authority["permissions"]],
            },
            "steps": [step.payload(index, identity) for index, step in enumerate(steps)],
        }

    def _sign(self, plan: dict[str, Any], identity: ProviderIdentity) -> str:
        secret = os.getenv(self.signing_secret_env, "")
        if len(secret.encode("utf-8")) < 32:
            raise RuntimeError(
                f"Contact Skill signing secret {self.signing_secret_env} is not configured"
            )
        now = time.time_ns() // 1000
        planned_duration = sum(
            float(step["delay_after_accept_s"]) for step in plan["steps"]
        ) + 15.0
        watchdog_coverage = min(
            sum(
                float(step["next_command_timeout_s"])
                for step in plan["steps"]
            ) + 5.0,
            60.0,
        )
        duration = max(planned_duration, watchdog_coverage)
        payload = {
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
            "expires_at_us": now + int(duration * 1_000_000),
        }
        payload_segment = _base64url(canonical_bytes(payload))
        signature = hmac.new(
            secret.encode("utf-8"),
            payload_segment.encode("ascii"),
            hashlib.sha256,
        ).digest()
        return f"{payload_segment}.{_base64url(signature)}"

    def execute(self, skill_id: str, steps: list[ContactStep]) -> dict[str, Any]:
        identity = self.provider_identity()
        execution_id = str(uuid.uuid4())
        authority = ManagerAuthorityLease(
            self.client,
            self.manager_url,
            identity.arm_resource_id,
            execution_id,
            skill_id,
        )
        authority_record = authority.acquire()
        session_submission_attempted = False
        move_results: list[dict[str, Any]] = []
        effective_holds_s: list[float] = []
        move_tracking_observations: list[dict[str, Any]] = []
        relax_result: dict[str, Any] | None = None
        authority_release_error: str | None = None
        try:
            plan = self.build_plan(
                skill_id,
                steps,
                identity=identity,
                manager_authority=authority_record,
                execution_id=execution_id,
            )
            assertion = self._sign(plan, identity)
            session_submission_attempted = True
            session = self.client.post(
                f"{self.provider_url}/v1/contact/session",
                {"plan": plan},
                {"X-Midbrain-Authorization": assertion},
            )
            session_id = str(session["session_id"])
            for step in plan["steps"]:
                authority.service()
                result = self.client.post(
                    f"{self.provider_url}/v1/contact/move",
                    {"session_id": session_id, "sequence": step["sequence"]},
                )
                move_results.append(result)
                effective_hold_s = self.effective_hold_s(
                    float(step["delay_after_accept_s"]),
                    result,
                )
                watchdog_s = float(step["next_command_timeout_s"])
                transition_s = float(
                    result.get("velocity_limited_transition_time_s", 0.0)
                )
                deadline_from_acceptance_s = transition_s + watchdog_s
                if effective_hold_s >= deadline_from_acceptance_s:
                    raise RuntimeError(
                        "Contact velocity-limited transition needs "
                        f"{effective_hold_s:.3f} s before the next command, but "
                        "the signed transition-plus-watchdog window is only "
                        f"{deadline_from_acceptance_s:.3f} s "
                        f"({transition_s:.3f} s + {watchdog_s:.3f} s)"
                    )
                effective_holds_s.append(effective_hold_s)
                deadline = time.monotonic() + effective_hold_s
                while time.monotonic() < deadline:
                    authority.service()
                    time.sleep(min(0.10, max(0.0, deadline - time.monotonic())))
                try:
                    provider_state = self.client.get(
                        f"{self.provider_url}/v1/contact/state"
                    )
                    move_tracking_observations.append(
                        {
                            "sequence": step["sequence"],
                            "active_sequence": provider_state.get(
                                "active_sequence"
                            ),
                            "cartesian_segment": provider_state.get(
                                "cartesian_segment"
                            ),
                            "joint_state_valid": provider_state.get(
                                "joint_state_valid"
                            ),
                        }
                    )
                except Exception as exc:
                    move_tracking_observations.append(
                        {
                            "sequence": step["sequence"],
                            "observation_error": str(exc),
                        }
                    )
        finally:
            if session_submission_attempted:
                try:
                    relax_result = self.client.post(
                        f"{self.provider_url}/v1/contact/relax",
                        {
                            "session_id": plan["execution_id"],
                            "reason": f"{skill_id} terminal cleanup",
                        },
                    )
                except Exception:
                    relax_result = {"disposition": "RELAX_REQUEST_FAILED"}
            try:
                authority.release(f"{skill_id} finished")
            except Exception as exc:
                authority_release_error = str(exc)
        return {
            "skill_id": skill_id,
            "execution_id": plan["execution_id"],
            "submitted_step_count": len(move_results),
            "move_dispositions": [item.get("disposition") for item in move_results],
            "move_results": move_results,
            "effective_holds_s": effective_holds_s,
            "move_tracking_observations": move_tracking_observations,
            "relax": relax_result,
            "manager_authority_lease_id": authority_record.get("lease_id"),
            "manager_authority_release_error": authority_release_error,
            "task_success_assessed": False,
            "result_semantics": "SEQUENCE_SUBMISSION_AND_RELAX_ONLY",
        }
