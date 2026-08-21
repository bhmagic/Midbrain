from __future__ import annotations

from typing import Any
from urllib.parse import quote
import math
import os
import time

from .runtime import JsonClient, canonical_sha256


def _finite_vector3(value: Any, name: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(f"{name} must contain three values")
    result = [float(component) for component in value]
    if not all(math.isfinite(component) for component in result):
        raise ValueError(f"{name} must contain finite values")
    return result


class ManagerDevelopmentRuntime:
    """Bounded Manager access for attended Skill development execution."""

    def __init__(self, base_url: str, client: JsonClient | None = None):
        self.base_url = base_url.rstrip("/")
        self.client = client or JsonClient(timeout_s=30.0)

    def assert_motion_allowed(self) -> dict[str, Any]:
        state = self.client.get(f"{self.base_url}/v1/motion/inhibit")
        if state.get("inhibited") is True:
            raise PermissionError(
                "global motion inhibit is active: "
                f"{state.get('owners') or state.get('reason') or 'unknown owner'}"
            )
        return state

    def set_hot(self, provider_id: str) -> dict[str, Any]:
        normalized = str(provider_id).strip()
        if not normalized:
            raise ValueError("provider_id must not be empty")
        return self.client.post(
            f"{self.base_url}/v1/providers/{quote(normalized, safe='')}/hot",
            {},
        )

    def set_residency(self, provider_id: str, action: str) -> dict[str, Any]:
        normalized_provider = str(provider_id).strip()
        normalized_action = str(action).strip().lower()
        if not normalized_provider:
            raise ValueError("provider_id must not be empty")
        if normalized_action not in {"start", "hot", "warm", "stop"}:
            raise ValueError("action must be start, hot, warm, or stop")
        return self.client.post(
            f"{self.base_url}/v1/providers/"
            f"{quote(normalized_provider, safe='')}/{normalized_action}",
            {},
        )

    def workcell_calibrations(self) -> dict[str, Any]:
        value = self.client.get(f"{self.base_url}/v1/workcell-calibrations")
        return value if isinstance(value, dict) else {"activations": []}


class IntegratedDevelopmentRuntime:
    """Use existing Integrated and host-authorization APIs without Agent reasoning."""

    def __init__(
        self,
        integrated_url: str,
        authorization_url: str,
        client: JsonClient | None = None,
        *,
        requester_id: str = "grip.development",
        operation_label: str = "Grip development",
        arm_base_frame: str | None = None,
    ) -> None:
        self.integrated_url = integrated_url.rstrip("/")
        self.authorization_url = authorization_url.rstrip("/")
        self.client = client or JsonClient(timeout_s=30.0)
        self.requester_id = str(requester_id).strip()
        self.operation_label = str(operation_label).strip()
        self.arm_base_frame = str(
            arm_base_frame or os.getenv("ARM_BASE_FRAME", "rebot_arm_base")
        ).strip()
        if not self.requester_id or not self.operation_label or not self.arm_base_frame:
            raise ValueError("Integrated development identity and arm-base frame are required")

    def state(self) -> dict[str, Any]:
        return self.client.get(f"{self.integrated_url}/v1/state")

    @staticmethod
    def _measured_controlled_frame(
        state: dict[str, Any],
    ) -> dict[str, Any]:
        model_view = state.get("model_view")
        if not isinstance(model_view, dict):
            controller = state.get("controller")
            controller = controller if isinstance(controller, dict) else {}
            model_view = controller.get("model_view")
        model_view = model_view if isinstance(model_view, dict) else {}
        measured = model_view.get("measured_controlled_frame")
        if not isinstance(measured, dict):
            raise RuntimeError(
                "Integrated has no measured controlled-effector frame"
            )
        return measured

    @staticmethod
    def measured_controlled_frame_position(
        state: dict[str, Any],
    ) -> list[float]:
        measured = IntegratedDevelopmentRuntime._measured_controlled_frame(state)
        try:
            return _finite_vector3(
                measured.get("position_m"),
                "Integrated measured controlled-effector position",
            )
        except ValueError as exc:
            raise RuntimeError(
                "Integrated has no finite measured controlled-effector position"
            ) from exc

    @staticmethod
    def measured_controlled_frame_pose(
        state: dict[str, Any],
    ) -> dict[str, list[float]]:
        measured = IntegratedDevelopmentRuntime._measured_controlled_frame(state)
        try:
            position = _finite_vector3(
                measured.get("position_m"),
                "Integrated measured controlled-effector position",
            )
            rpy = _finite_vector3(
                measured.get("rpy_rad"),
                "Integrated measured controlled-effector orientation",
            )
        except ValueError as exc:
            raise RuntimeError(
                "Integrated has no finite measured controlled-effector pose"
            ) from exc
        return {"position_m": position, "rpy_rad": rpy}

    @staticmethod
    def identity(state: dict[str, Any]) -> dict[str, str]:
        value = state.get("controller_identity")
        if not isinstance(value, dict):
            raise RuntimeError("Integrated controller identity is unavailable")
        fields = (
            "provider_id",
            "provider_instance_id",
            "boot_id",
            "configuration_sha256",
        )
        identity = {field: str(value.get(field) or "") for field in fields}
        if any(not item for item in identity.values()):
            raise RuntimeError("Integrated controller identity is incomplete")
        return identity

    def preview_rotation(
        self,
        *,
        target_rpy_rad: list[float],
        calibration_binding: dict[str, Any],
    ) -> dict[str, Any]:
        state = self.state()
        if state.get("ready") is not True or state.get("residency") != "HOT":
            raise RuntimeError("Integrated must be HOT and ready before preview")
        if not isinstance(calibration_binding, dict) or not calibration_binding:
            raise ValueError("calibration_binding must be a non-empty object")
        target_position_m = self.measured_controlled_frame_position(state)
        target_rpy = _finite_vector3(target_rpy_rad, "target_rpy_rad")
        spatial_resolution = {
            "schema": "physical_agent.semantic_direction_resolution",
            "schema_version": 2,
            "convention_id": "MIDBRAIN_X_FORWARD_Y_LEFT_Z_UP_V2",
            "direction": "NONE",
            "reference_frame": "CONTROLLED_FRAME",
            "resolved_frame": self.arm_base_frame,
            "resolved_unit_vector": [0.0, 0.0, 0.0],
            "provenance": {
                "resolution_source": "ROTATION_ONLY_NO_TRANSLATION",
                "arm_base_frame": self.arm_base_frame,
            },
        }
        resolved_at_us = time.time_ns() // 1000
        result = self.client.post(
            f"{self.integrated_url}/v1/motion/path-plan",
            {
                "target": {
                    "position_m": target_position_m,
                    "rpy_rad": target_rpy,
                },
                "requested_speed_m_s": 0.05,
                "execution_backend": "IMPEDANCE",
                "ik_mode": "POSE_6DOF",
                "allowed_contact_object_ids": [],
                "permit_pushable_contact": False,
                "final_state": "FLOAT",
                "request_context": {
                    "context_kind": "AUTONOMOUS_FREE_SPACE_KINEMATIC",
                    "spatial_resolution_sha256": canonical_sha256(
                        spatial_resolution
                    ),
                    "spatial_resolution_resolved_at_us": resolved_at_us,
                    "spatial_resolution": spatial_resolution,
                },
            },
        )
        selected = result.get("selected_plan")
        selected = selected if isinstance(selected, dict) else {}
        selected_preview = selected.get("preview")
        selected_preview = (
            selected_preview if isinstance(selected_preview, dict) else {}
        )
        contract = result.get("preview_contract")
        contract = contract if isinstance(contract, dict) else {}
        if (
            result.get("status") != "PLANNED"
            or selected.get("planning_valid") is not True
            or selected_preview.get("collision_free") is not True
            or result.get("closest_safe") is True
            or selected.get("closest_safe") is True
            or contract.get("request_context_complete") is not True
            or not str(result.get("plan_id") or "").strip()
        ):
            reasons = selected.get("planning_reasons") or contract.get(
                "request_context_issues"
            )
            raise RuntimeError(
                f"Integrated rejected the {self.operation_label} rotation preview: "
                f"{reasons or result.get('status') or 'unknown reason'}"
            )
        return {
            "preview": result,
            "controller_identity": self.identity(state),
            "prepared_at_us": resolved_at_us,
            "rotation_only": True,
            "measured_start_position_m": target_position_m,
        }

    def execute(self, prepared: dict[str, Any]) -> dict[str, Any]:
        preview = prepared["preview"]
        current_identity = self.identity(self.state())
        if current_identity != prepared["controller_identity"]:
            raise RuntimeError(
                "Integrated identity changed after preview; prepare again"
            )
        contract = preview.get("preview_contract")
        contract = contract if isinstance(contract, dict) else {}
        plan_id = str(preview.get("plan_id") or "").strip()
        authority = {
            "plan_id": plan_id,
            "request_sha256": contract.get("request_sha256"),
            "preview_sha256": contract.get("preview_sha256"),
            "controller_provider_id": contract.get("controller_provider_id"),
            "controller_provider_instance_id": contract.get(
                "controller_provider_instance_id"
            ),
            "controller_boot_id": contract.get("controller_boot_id"),
            "controller_configuration_sha256": contract.get(
                "controller_configuration_sha256"
            ),
            "issued_at_us": contract.get("issued_at_us"),
            "expires_at_us": contract.get("expires_at_us"),
            "scene_revision": contract.get("scene_revision"),
        }
        remaining_s = (
            int(authority.get("expires_at_us") or 0) - time.time_ns() // 1000
        ) / 1_000_000.0
        if remaining_s <= 0.2:
            raise RuntimeError("Integrated preview expired; prepare again")
        decision = self.client.post(
            f"{self.authorization_url}/api/authorizations",
            {
                "requester_type": "SKILL",
                "requester_id": self.requester_id,
                "decision_type": "PHYSICAL_OBSERVATION_POSE",
                "title": f"Execute {self.operation_label} rotation alignment",
                "summary": (
                    "Execute the exact collision-checked Integrated rotation-only "
                    f"alignment accepted by the attended {self.operation_label} operator."
                ),
                "proposed_action": preview,
                "evidence": {
                    "developer_attended": True,
                    "physical_stage_acknowledged": True,
                },
                "safety": {
                    "physical_motion": True,
                    "approval_executes_action": False,
                    "controller_preview_required": True,
                    "controller_preview_authority": authority,
                    "contact_policy": "NO_CONTACT",
                    "human_approval_required": True,
                },
                "expires_in_s": max(1.0, min(30.0, remaining_s)),
            },
        )
        decision_id = str(decision["decision_id"])
        approved = self.client.post(
            f"{self.authorization_url}/api/authorizations/{decision_id}/resolve",
            {
                "resolution": "APPROVED",
                "resolved_by": f"{self.requester_id}-operator",
                "note": "Approved by the explicit physical-stage acknowledgement.",
            },
        )
        assertion = self.client.post(
            f"{self.authorization_url}/api/authorizations/"
            f"{decision_id}/execution-assertion",
            {},
        )
        commit = self.client.post(
            f"{self.integrated_url}/v1/motion/path-commit",
            {
                "plan_id": plan_id,
                "request_sha256": authority["request_sha256"],
                "preview_sha256": authority["preview_sha256"],
                "decision_id": decision_id,
                "authorization_assertion_sha256": assertion[
                    "assertion_sha256"
                ],
            },
            {"X-Midbrain-Authorization": str(assertion["assertion"])},
        )
        if commit.get("status") != "EXECUTING":
            raise RuntimeError("Integrated did not enter EXECUTING after commit")
        planned_duration_s = float(commit.get("planned_duration_s") or 0.0)
        deadline = time.monotonic() + min(
            75.0,
            max(12.0, planned_duration_s + 15.0),
        )
        try:
            while time.monotonic() < deadline:
                state = self.state()
                planning = state.get("planning")
                planning = planning if isinstance(planning, dict) else {}
                active = planning.get("authorized_transit")
                if isinstance(active, dict):
                    if str(active.get("plan_id") or "") != plan_id:
                        raise RuntimeError(
                            "Integrated reports a different active transit"
                        )
                    if str(active.get("status") or "").upper() in {
                        "FAILED",
                        "CANCELLED",
                    }:
                        raise RuntimeError(
                            "Integrated alignment failed: "
                            f"{active.get('error') or active.get('status')}"
                        )
                else:
                    completed = planning.get("last_authorized_transit")
                    if isinstance(completed, dict):
                        status = str(completed.get("status") or "").upper()
                        if status in {"FAILED", "CANCELLED"}:
                            raise RuntimeError(
                                "Integrated alignment failed: "
                                f"{completed.get('error') or status}"
                            )
                        if status == "COMPLETED_FLOAT":
                            if (state.get("safety") or {}).get(
                                "float_confirmed"
                            ) is not True:
                                raise RuntimeError(
                                    "Integrated completed without verified FLOAT"
                                )
                            return {
                                "preview_id": plan_id,
                                "decision_id": decision_id,
                                "authorization_status": approved.get("status"),
                                "commit": commit,
                                "completion": completed,
                                "final_state": "FLOAT",
                                "goal_reached": True,
                            }
                time.sleep(0.1)
            raise TimeoutError(
                "Integrated alignment did not reach verified FLOAT"
            )
        except Exception:
            try:
                self.client.post(
                    f"{self.integrated_url}/v1/motion/path-release",
                    {},
                )
            except Exception:
                pass
            raise

    def handoff_to_contact(
        self,
        manager: ManagerDevelopmentRuntime,
        contact_provider_id: str,
    ) -> dict[str, Any]:
        """Perform Slicing's verified Integrated-WARM to Contact-HOT handoff."""

        manager.set_residency("robot_arm.primary.integrated", "warm")
        state = self.state()
        controller = state.get("controller")
        controller = controller if isinstance(controller, dict) else state
        safety = controller.get("safety")
        safety = safety if isinstance(safety, dict) else {}
        trajectory = controller.get("trajectory")
        trajectory = trajectory if isinstance(trajectory, dict) else {}
        lease = controller.get("lease")
        lease = lease if isinstance(lease, dict) else {}
        residency = str(controller.get("residency") or "").upper()
        if (
            residency != "WARM"
            or safety.get("float_confirmed") is not True
            or trajectory.get("active") is not False
            or lease.get("active") is not False
        ):
            raise RuntimeError(
                "Integrated-to-Contact handoff was not confirmed: "
                f"integrated_residency={residency or 'UNKNOWN'}, "
                f"float_confirmed={safety.get('float_confirmed')!r}, "
                f"trajectory_active={trajectory.get('active')!r}, "
                f"integrated_basic_lease_active={lease.get('active')!r}; "
                "Contact was not started"
            )
        manager.set_hot(contact_provider_id)
        return {
            "integrated_residency": residency,
            "float_confirmed": True,
            "trajectory_active": False,
            "integrated_basic_lease_active": False,
            "contact_provider_id": str(contact_provider_id),
            "contact_hot_requested": True,
        }


def active_motion_calibration(document: dict[str, Any]) -> dict[str, Any]:
    values = [
        value
        for value in document.get("activations", [])
        if isinstance(value, dict)
        and value.get("state") == "ACTIVE"
        and value.get("motion_usable") is True
        and value.get("expires_at") is None
        and value.get("expires_at_us") is None
    ]
    if len(values) != 1:
        raise RuntimeError(
            "development execution requires exactly one active motion-usable calibration"
        )
    return values[0]


def provider_identity(state: dict[str, Any]) -> dict[str, str]:
    fields = (
        "provider_id",
        "provider_instance_id",
        "provider_boot_id",
        "assembly_fingerprint",
        "mounted_effector_revision",
    )
    identity = {field: str(state.get(field) or "") for field in fields}
    if any(not value for value in identity.values()):
        raise RuntimeError("Provider identity is incomplete")
    return identity


def assert_provider_identity(
    expected: dict[str, str],
    current_state: dict[str, Any],
) -> None:
    current = provider_identity(current_state)
    if current != expected:
        raise RuntimeError(
            "Provider identity changed after development preparation; prepare again"
        )


def development_session(
    *,
    session_id: str,
    stage_definitions: list[dict[str, Any]],
    frozen_plan: dict[str, Any],
) -> dict[str, Any]:
    now_us = time.time_ns() // 1000
    return {
        "session_id": session_id,
        "status": "PREPARED",
        "prepared_at_us": now_us,
        "updated_at_us": now_us,
        "next_stage_number": 1,
        "next_stage_deadline_at_us": None,
        "stage_definitions": stage_definitions,
        "stage_results": [],
        "frozen_plan": frozen_plan,
        "error": None,
    }


def public_session(session: dict[str, Any] | None) -> dict[str, Any] | None:
    if session is None:
        return None
    return {
        key: value
        for key, value in session.items()
        if not str(key).startswith("_")
    }
