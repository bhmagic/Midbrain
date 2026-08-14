from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
import asyncio
import copy
import json
import math
import os
import time
import uuid

import numpy as np

from contact_work_runtime import ContactWorkRuntime
from slicing_skill import (
    ABSOLUTE_WORLD_POINT_MODE,
    BLADE_PROFILE_EXTENSION_ID,
    RELATIVE_WORLD_POINT_MODE,
    SKILL_ID,
    SlicingPlan,
    blade_profiles_from_effector,
    build_slicing_plan,
    motion_profiles_from_document,
    resolve_point_arguments,
    select_active_workcell_activation,
)
from slicing_skill.skill import activation_binding, same_activation_binding


_ACTIVE_DEVELOPMENT_STATES = {
    "PREVIEW_READY",
    "ALIGNMENT_IN_PROGRESS",
    "CONTACT_READY",
    "CONTACT_IN_PROGRESS",
}
_MAX_FLOAT_HANDOFF_ORIENTATION_DRIFT_RAD = 0.35


class ContactPreflightRejected(RuntimeError):
    """Reject Contact before provider activation or Contact authorization."""


@dataclass
class _DevelopmentSession:
    session_id: str
    state: str
    created_monotonic: float
    updated_monotonic: float
    original_arguments: dict[str, Any]
    resolved_arguments: dict[str, Any]
    point_resolution: dict[str, Any]
    plan: SlicingPlan
    integrated_preview: dict[str, Any]
    alignment_result: dict[str, Any] | None = None
    contact_result: dict[str, Any] | None = None
    error: str | None = None

    def snapshot(self, *, now: float, ttl_s: float) -> dict[str, Any]:
        active = self.state in _ACTIVE_DEVELOPMENT_STATES
        return {
            "session_id": self.session_id,
            "state": self.state,
            "expires_in_s": (
                max(0.0, ttl_s - (now - self.updated_monotonic))
                if active
                else None
            ),
            "original_arguments": copy.deepcopy(self.original_arguments),
            "resolved_arguments": copy.deepcopy(self.resolved_arguments),
            "point_resolution": copy.deepcopy(self.point_resolution),
            "plan": self.plan.as_dict(),
            "integrated_preview": copy.deepcopy(self.integrated_preview),
            "alignment_result": copy.deepcopy(self.alignment_result),
            "contact_result": copy.deepcopy(self.contact_result),
            "error": self.error,
            "task_success_assessed": False,
        }


class SlicingHostAdapter:
    def __init__(
        self,
        *,
        manager: Any,
        fabric: Any,
        integrated_motion: Any,
        contact_provider_url: str,
        effector_profile_path: Path,
        motion_profiles_path: Path,
        motion_profiles_template_path: Path,
        contact_runtime_factory: Callable[[], Any] | None = None,
        development_session_ttl_s: float = 120.0,
    ):
        if integrated_motion is None:
            raise RuntimeError("Slicing requires the bound Integrated motion adapter")
        if (
            not math.isfinite(development_session_ttl_s)
            or development_session_ttl_s <= 0.0
        ):
            raise ValueError("development_session_ttl_s must be positive")
        self.manager = manager
        self.fabric = fabric
        self.integrated_motion = integrated_motion
        self.contact_provider_url = contact_provider_url.rstrip("/")
        self.effector_profile_path = Path(effector_profile_path).resolve()
        self.motion_profiles_path = Path(motion_profiles_path).resolve()
        self.motion_profiles_template_path = Path(
            motion_profiles_template_path
        ).resolve()
        self.contact_runtime_factory = contact_runtime_factory or self._runtime
        self.development_session_ttl_s = float(development_session_ttl_s)
        self._development_sessions: dict[str, _DevelopmentSession] = {}
        self._active_development_session_id: str | None = None
        self._agent_workflow_active = False
        self._state_lock = asyncio.Lock()
        self._workflow_lock = asyncio.Lock()
        self._profile_lock = asyncio.Lock()

    def _runtime(self) -> ContactWorkRuntime:
        return ContactWorkRuntime(
            self.contact_provider_url,
            self.manager.base_url,
            signing_secret_env="MIDBRAIN_CONTACT_SLICING_SECRET",
        )

    async def invoke(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Execute both stages for the autonomous Agent entrypoint."""

        async with self._workflow_lock:
            async with self._state_lock:
                self._prune_development_locked(time.monotonic())
                if self._active_development_session_id is not None:
                    raise RuntimeError(
                        "a slicing developer session owns the staged workflow"
                    )
                self._agent_workflow_active = True
            try:
                invocation_arguments = copy.deepcopy(arguments)
                point_mode = str(
                    invocation_arguments.pop(
                        "point_mode",
                        ABSOLUTE_WORLD_POINT_MODE,
                    )
                )
                prepared = await self._prepare_core(
                    invocation_arguments,
                    point_mode=point_mode,
                )
                alignment_result = await self._execute_alignment_core(
                    prepared["plan"],
                    prepared["preview_id"],
                )
                contact_result = await self._execute_contact_core(
                    prepared["plan"],
                    alignment_result,
                )
            finally:
                async with self._state_lock:
                    self._agent_workflow_active = False
        return self._completed_result(
            prepared["plan"],
            alignment_result,
            contact_result,
        )

    async def prepare_development(
        self,
        arguments: dict[str, Any],
        *,
        point_mode: str,
    ) -> dict[str, Any]:
        """Build one exact plan and Integrated preview without moving the arm."""

        async with self._workflow_lock:
            async with self._state_lock:
                self._prune_development_locked(time.monotonic())
                if self._agent_workflow_active:
                    raise RuntimeError("an Agent slicing workflow is active")
                if self._active_development_session_id is not None:
                    raise RuntimeError(
                        "finish or cancel the active slicing developer session first"
                    )
            prepared = await self._prepare_core(
                arguments,
                point_mode=point_mode,
            )
            now = time.monotonic()
            session = _DevelopmentSession(
                session_id=uuid.uuid4().hex,
                state="PREVIEW_READY",
                created_monotonic=now,
                updated_monotonic=now,
                original_arguments=copy.deepcopy(arguments),
                resolved_arguments=prepared["resolved_arguments"],
                point_resolution=prepared["point_resolution"],
                plan=prepared["plan"],
                integrated_preview=prepared["preview"],
            )
            async with self._state_lock:
                self._development_sessions[session.session_id] = session
                self._active_development_session_id = session.session_id
                self._trim_sessions_locked()
                return session.snapshot(
                    now=now,
                    ttl_s=self.development_session_ttl_s,
                )

    async def execute_development_alignment(
        self,
        session_id: str,
    ) -> dict[str, Any]:
        """Execute Stage 1 through the existing Integrated signed path."""

        async with self._workflow_lock:
            session = await self._begin_development_stage(
                session_id,
                required_state="PREVIEW_READY",
                in_progress_state="ALIGNMENT_IN_PROGRESS",
            )
            try:
                preview_id = str(
                    session.integrated_preview.get("preview_id") or ""
                ).strip()
                result = await self._execute_alignment_core(
                    session.plan,
                    preview_id,
                )
            except Exception as error:
                await self._fail_development_session(
                    session,
                    state="ALIGNMENT_FAILED",
                    error=error,
                )
                raise
            async with self._state_lock:
                session.alignment_result = copy.deepcopy(result)
                session.state = "CONTACT_READY"
                session.updated_monotonic = time.monotonic()
                return session.snapshot(
                    now=session.updated_monotonic,
                    ttl_s=self.development_session_ttl_s,
                )

    async def execute_development_contact(
        self,
        session_id: str,
    ) -> dict[str, Any]:
        """Execute Stage 2 through Contact and its terminal relax cleanup."""

        async with self._workflow_lock:
            session = await self._begin_development_stage(
                session_id,
                required_state="CONTACT_READY",
                in_progress_state="CONTACT_IN_PROGRESS",
            )
            try:
                result = await self._execute_contact_core(
                    session.plan,
                    session.alignment_result,
                )
            except ContactPreflightRejected as error:
                await self._fail_development_session(
                    session,
                    state="CONTACT_NOT_STARTED_PREFLIGHT_REJECTED",
                    error=error,
                )
                raise
            except Exception as error:
                await self._fail_development_session(
                    session,
                    state="CONTACT_FAILED_RELAX_REQUESTED",
                    error=error,
                )
                raise
            async with self._state_lock:
                session.contact_result = copy.deepcopy(result)
                session.state = "COMPLETE_RELAX_REQUESTED"
                session.updated_monotonic = time.monotonic()
                self._active_development_session_id = None
                return session.snapshot(
                    now=session.updated_monotonic,
                    ttl_s=self.development_session_ttl_s,
                )

    async def cancel_development(self, session_id: str) -> dict[str, Any]:
        """Cancel a staged session only while no stage execution is running."""

        async with self._workflow_lock:
            async with self._state_lock:
                self._prune_development_locked(time.monotonic())
                session = self._require_session_locked(session_id)
                if session.state not in {"PREVIEW_READY", "CONTACT_READY"}:
                    raise RuntimeError(
                        f"slicing session cannot be canceled from {session.state}"
                    )
                session.state = "CANCELED_NO_CONTACT"
                session.updated_monotonic = time.monotonic()
                if self._active_development_session_id == session.session_id:
                    self._active_development_session_id = None
                return session.snapshot(
                    now=session.updated_monotonic,
                    ttl_s=self.development_session_ttl_s,
                )

    async def development_observation(self) -> dict[str, Any]:
        """Return staged state and editable profiles without moving hardware."""

        blade_profile_status = await self._blade_profile_status()
        motion_profiles = self._motion_profiles()
        arm_joint_names = await self._active_arm_joint_names()
        now = time.monotonic()
        async with self._state_lock:
            self._prune_development_locked(now)
            sessions = sorted(
                self._development_sessions.values(),
                key=lambda value: value.updated_monotonic,
                reverse=True,
            )
            return {
                "skill_id": SKILL_ID,
                "agent_workflow_active": self._agent_workflow_active,
                "active_development_session_id": (
                    self._active_development_session_id
                ),
                "session_ttl_s": self.development_session_ttl_s,
                "point_modes": [
                    ABSOLUTE_WORLD_POINT_MODE,
                    RELATIVE_WORLD_POINT_MODE,
                ],
                "blade_profiles": blade_profile_status["source"]["profiles"],
                "default_blade_profile_number": blade_profile_status["source"][
                    "default_profile_number"
                ],
                "active_blade_profiles": blade_profile_status["active"][
                    "profiles"
                ],
                "blade_profiles_pending_workspace_restart": (
                    blade_profile_status["pending_workspace_restart"]
                ),
                "agent_blade_profile_selection_live": (
                    blade_profile_status["agent_profile_selection_live"]
                ),
                "motion_profiles": motion_profiles["profiles"],
                "default_motion_profile_number": motion_profiles[
                    "default_profile_number"
                ],
                "arm_joint_names": arm_joint_names,
                "sessions": [
                    session.snapshot(
                        now=now,
                        ttl_s=self.development_session_ttl_s,
                    )
                    for session in sessions[:12]
                ],
                "task_success_assessed": False,
            }

    async def _prepare_core(
        self,
        arguments: dict[str, Any],
        *,
        point_mode: str,
    ) -> dict[str, Any]:
        await self.manager.set_hot("robot_arm.primary.integrated")
        calibration_document = await self.manager.workcell_calibrations()
        activation = select_active_workcell_activation(calibration_document)
        effector_profile = await self._effective_slicing_effector_profile()
        motion_profiles = self._motion_profiles()
        current_position: list[float] | None = None
        if str(point_mode or "").strip().upper() == RELATIVE_WORLD_POINT_MODE:
            current_position = await self._measured_current_effector_arm_base()
        resolved_arguments, point_resolution = resolve_point_arguments(
            arguments,
            activation,
            point_mode=point_mode,
            current_effector_arm_base_m=current_position,
        )
        plan = build_slicing_plan(
            resolved_arguments,
            activation,
            effector_profile=effector_profile,
            motion_profiles_document=motion_profiles,
        )
        preview = await self.integrated_motion.preview(
            **plan.integrated_alignment_arguments
        )
        if preview.get("status") != "PREVIEW_READY":
            raise RuntimeError(
                self._preview_rejection_message(preview)
            )
        preview_id = str(preview.get("preview_id") or "").strip()
        if not preview_id:
            raise RuntimeError("Integrated slicing alignment preview has no ID")
        return {
            "plan": plan,
            "preview": copy.deepcopy(preview),
            "preview_id": preview_id,
            "resolved_arguments": resolved_arguments,
            "point_resolution": point_resolution,
        }

    async def _execute_alignment_core(
        self,
        plan: SlicingPlan,
        preview_id: str,
    ) -> dict[str, Any]:
        if not preview_id:
            raise RuntimeError("Integrated slicing alignment preview has no ID")
        await self._require_current_calibration(plan)
        alignment_result = await self.integrated_motion.execute_preview(
            preview_id=preview_id
        )
        if (
            alignment_result.get("workflow_complete") is not True
            or alignment_result.get("physical_motion_completed") is not True
            or alignment_result.get("goal_reached") is not True
            or str(alignment_result.get("final_state") or "").upper() != "FLOAT"
        ):
            raise RuntimeError(
                "Integrated slicing alignment did not complete its accepted target in FLOAT"
            )
        await self._require_current_calibration(plan)
        handoff = await self._capture_integrated_handoff(alignment_result)
        result = copy.deepcopy(alignment_result)
        result["slicing_handoff"] = handoff
        return result

    async def _execute_contact_core(
        self,
        plan: SlicingPlan,
        alignment_result: dict[str, Any] | None,
    ) -> dict[str, Any]:
        await self._require_current_calibration(plan)
        await self._require_aligned_integrated_float(alignment_result)
        await self._handoff_integrated_lease_to_contact()
        runtime = self.contact_runtime_factory()
        result = await asyncio.to_thread(
            runtime.execute,
            SKILL_ID,
            list(plan.contact_steps),
        )
        return copy.deepcopy(result)

    async def _require_current_calibration(self, plan: SlicingPlan) -> None:
        current_document = await self.manager.workcell_calibrations()
        current_activation = select_active_workcell_activation(current_document)
        if not same_activation_binding(
            plan.workcell_binding,
            activation_binding(current_activation),
        ):
            raise RuntimeError(
                "workcell calibration changed; the next slicing stage was not started"
            )

    async def _require_aligned_integrated_float(
        self,
        alignment_result: dict[str, Any] | None,
    ) -> None:
        if not isinstance(alignment_result, dict):
            raise ContactPreflightRejected(
                "Integrated handoff evidence is missing; Contact was not started"
            )
        handoff = alignment_result.get("slicing_handoff")
        handoff = handoff if isinstance(handoff, dict) else {}
        observation = await self.integrated_motion.observation()
        controller = observation.get("controller")
        controller = controller if isinstance(controller, dict) else {}
        safety = controller.get("safety")
        safety = safety if isinstance(safety, dict) else {}
        trajectory = controller.get("trajectory")
        trajectory = trajectory if isinstance(trajectory, dict) else {}
        model_view = controller.get("model_view")
        model_view = model_view if isinstance(model_view, dict) else {}
        measured = model_view.get("measured_controlled_frame")
        measured = measured if isinstance(measured, dict) else {}
        measured_rpy = measured.get("rpy_rad")
        handoff_rpy = handoff.get("measured_rpy_rad")
        planning = controller.get("planning")
        planning = planning if isinstance(planning, dict) else {}
        last_completed = planning.get("last_authorized_transit")
        last_completed = (
            last_completed if isinstance(last_completed, dict) else {}
        )
        expected_plan_id = str(handoff.get("plan_id") or "").strip()
        current_plan_id = str(last_completed.get("plan_id") or "").strip()
        expected_boot_id = str(handoff.get("controller_boot_id") or "").strip()
        current_boot_id = str(controller.get("boot_id") or "").strip()
        expected_instance_id = str(
            handoff.get("controller_provider_instance_id") or ""
        ).strip()
        current_instance_id = str(
            controller.get("provider_instance_id") or ""
        ).strip()
        orientation_drift = (
            self._orientation_distance_rad(measured_rpy, handoff_rpy)
            if self._finite_vector3(measured_rpy)
            and self._finite_vector3(handoff_rpy)
            else math.inf
        )
        if (
            safety.get("float_confirmed") is not True
            or trajectory.get("active") is not False
            or not self._finite_vector3(measured_rpy)
            or not self._finite_vector3(handoff_rpy)
            or orientation_drift > _MAX_FLOAT_HANDOFF_ORIENTATION_DRIFT_RAD
            or (expected_plan_id and current_plan_id != expected_plan_id)
            or (expected_boot_id and current_boot_id != expected_boot_id)
            or (
                expected_instance_id
                and current_instance_id != expected_instance_id
            )
        ):
            raise ContactPreflightRejected(
                "Integrated-to-Contact preflight rejected the handoff: "
                f"float_confirmed={safety.get('float_confirmed')!r}, "
                f"trajectory_active={trajectory.get('active')!r}, "
                f"orientation_drift_rad={orientation_drift:.6f}, "
                "maximum_orientation_drift_rad="
                f"{_MAX_FLOAT_HANDOFF_ORIENTATION_DRIFT_RAD:.6f}, "
                f"expected_plan_id={expected_plan_id or 'UNKNOWN'}, "
                f"current_plan_id={current_plan_id or 'UNKNOWN'}, "
                f"controller_identity_unchanged="
                f"{(not expected_boot_id or current_boot_id == expected_boot_id) and (not expected_instance_id or current_instance_id == expected_instance_id)}; "
                "Contact was not started"
            )

    async def _handoff_integrated_lease_to_contact(self) -> None:
        try:
            await self.manager.set_residency(
                "robot_arm.primary.integrated",
                "warm",
            )
            observation = await self.integrated_motion.observation()
            controller = observation.get("controller")
            controller = controller if isinstance(controller, dict) else {}
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
                raise ContactPreflightRejected(
                    "Integrated-to-Contact lease handoff was not confirmed: "
                    f"integrated_residency={residency or 'UNKNOWN'}, "
                    f"float_confirmed={safety.get('float_confirmed')!r}, "
                    f"trajectory_active={trajectory.get('active')!r}, "
                    f"integrated_basic_lease_active={lease.get('active')!r}; "
                    "Contact was not started"
                )
            await self.manager.set_hot("robot_arm.primary.contact")
        except ContactPreflightRejected:
            raise
        except Exception as error:
            raise ContactPreflightRejected(
                "Integrated-to-Contact provider transition failed before a "
                f"Contact session was submitted: {error}"
            ) from error

    async def _capture_integrated_handoff(
        self,
        alignment_result: dict[str, Any],
    ) -> dict[str, Any]:
        observation = await self.integrated_motion.observation()
        controller = observation.get("controller")
        controller = controller if isinstance(controller, dict) else {}
        safety = controller.get("safety")
        safety = safety if isinstance(safety, dict) else {}
        trajectory = controller.get("trajectory")
        trajectory = trajectory if isinstance(trajectory, dict) else {}
        model_view = controller.get("model_view")
        model_view = model_view if isinstance(model_view, dict) else {}
        measured = model_view.get("measured_controlled_frame")
        measured = measured if isinstance(measured, dict) else {}
        planning = controller.get("planning")
        planning = planning if isinstance(planning, dict) else {}
        last_completed = planning.get("last_authorized_transit")
        last_completed = (
            last_completed if isinstance(last_completed, dict) else {}
        )
        measured_position = measured.get("position_m")
        measured_rpy = measured.get("rpy_rad")
        expected_plan_id = str(
            alignment_result.get("controller_preview_id")
            or alignment_result.get("preview_id")
            or ""
        ).strip()
        completed_plan_id = str(last_completed.get("plan_id") or "").strip()
        if (
            safety.get("float_confirmed") is not True
            or trajectory.get("active") is not False
            or not self._finite_vector3(measured_position)
            or not self._finite_vector3(measured_rpy)
            or not expected_plan_id
            or completed_plan_id != expected_plan_id
        ):
            raise RuntimeError(
                "Integrated completed Stage 1 but did not expose a stable, "
                "identity-bound FLOAT handoff"
            )
        return {
            "plan_id": expected_plan_id,
            "controller_boot_id": str(controller.get("boot_id") or "") or None,
            "controller_provider_instance_id": str(
                controller.get("provider_instance_id") or ""
            )
            or None,
            "measured_position_m": [
                float(value) for value in measured_position
            ],
            "measured_rpy_rad": [float(value) for value in measured_rpy],
            "float_confirmed": True,
            "trajectory_active": False,
            "maximum_orientation_drift_rad": (
                _MAX_FLOAT_HANDOFF_ORIENTATION_DRIFT_RAD
            ),
        }

    async def save_development_blade_profile(
        self,
        *,
        name: str | None,
        blade_direction_effector: list[float],
        slicing_direction_effector: list[float],
        locked_joint_names: list[str],
    ) -> dict[str, Any]:
        await self._require_profile_editable()
        arm_joint_names = await self._active_arm_joint_names()
        normalized_locks = [str(value).strip() for value in locked_joint_names]
        unknown_locks = sorted(set(normalized_locks) - set(arm_joint_names))
        if unknown_locks:
            raise ValueError(
                "locked_joint_names contains joints outside the active arm group: "
                + ", ".join(unknown_locks)
            )
        async with self._profile_lock:
            source = self._source_effector_profile()
            active = await self._active_effector_profile()
            self._require_same_effector_identity(source, active)
            normalized = blade_profiles_from_effector(source)
            number = self._next_available_profile_number(
                item["profile_number"] for item in normalized["profiles"]
            )
            record = {
                "profile_number": number,
                "name": self._profile_name(name, "Blade", number),
                "blade_direction_effector": [
                    float(value) for value in blade_direction_effector
                ],
                "slicing_direction_effector": [
                    float(value) for value in slicing_direction_effector
                ],
                "locked_joint_names": normalized_locks,
            }
            extension = source["extensions"][BLADE_PROFILE_EXTENSION_ID]
            extension["profiles"].append(record)
            if extension.get("default_profile_number") is None:
                extension["default_profile_number"] = number
            blade_profiles_from_effector(source)
            self._write_json_atomic(self.effector_profile_path, source)
        return {
            "saved": copy.deepcopy(record),
            "profile_status": await self._blade_profile_status(),
        }

    async def delete_development_blade_profile(
        self,
        profile_number: int,
    ) -> dict[str, Any]:
        number = self._profile_number(profile_number, "blade")
        await self._require_profile_editable()
        async with self._profile_lock:
            source = self._source_effector_profile()
            active = await self._active_effector_profile()
            self._require_same_effector_identity(source, active)
            extension = source["extensions"][BLADE_PROFILE_EXTENSION_ID]
            before = list(extension["profiles"])
            extension["profiles"] = [
                value
                for value in before
                if value.get("profile_number") != number
            ]
            if len(extension["profiles"]) == len(before):
                raise KeyError(f"blade profile #{number} was not found")
            if extension.get("default_profile_number") == number:
                extension["default_profile_number"] = self._lowest_profile_number(
                    extension["profiles"]
                )
            blade_profiles_from_effector(source)
            self._write_json_atomic(self.effector_profile_path, source)
        return {
            "deleted_profile_number": number,
            "profile_status": await self._blade_profile_status(),
        }

    async def set_development_blade_profile_default(
        self,
        profile_number: int,
    ) -> dict[str, Any]:
        number = self._profile_number(profile_number, "blade")
        await self._require_profile_editable()
        async with self._profile_lock:
            source = self._source_effector_profile()
            active = await self._active_effector_profile()
            self._require_same_effector_identity(source, active)
            extension = source["extensions"][BLADE_PROFILE_EXTENSION_ID]
            if not any(
                value.get("profile_number") == number
                for value in extension["profiles"]
            ):
                raise KeyError(f"blade profile #{number} was not found")
            extension["default_profile_number"] = number
            blade_profiles_from_effector(source)
            self._write_json_atomic(self.effector_profile_path, source)
        return {
            "default_profile_number": number,
            "profile_status": await self._blade_profile_status(),
        }

    async def save_development_motion_profile(
        self,
        *,
        name: str | None,
        blade_load_kgf: float,
        retract_distance_m: float,
        delay_after_engage_s: float,
        slice_wait_speed_m_s: float,
        delay_after_retract_s: float,
    ) -> dict[str, Any]:
        await self._require_profile_editable()
        async with self._profile_lock:
            document = self._motion_profile_document_for_edit()
            normalized = motion_profiles_from_document(document)
            number = self._next_available_profile_number(
                item["profile_number"] for item in normalized["profiles"]
            )
            record = {
                "profile_number": number,
                "name": self._profile_name(name, "Motion", number),
                "blade_load_kgf": float(blade_load_kgf),
                "retract_distance_m": float(retract_distance_m),
                "delay_after_engage_s": float(delay_after_engage_s),
                "slice_wait_speed_m_s": float(slice_wait_speed_m_s),
                "delay_after_retract_s": float(delay_after_retract_s),
            }
            document["profiles"].append(record)
            if document.get("default_profile_number") is None:
                document["default_profile_number"] = number
            motion_profiles_from_document(document)
            self._write_json_atomic(self.motion_profiles_path, document)
        return {"saved": copy.deepcopy(record), "motion_profiles": self._motion_profiles()}

    async def delete_development_motion_profile(
        self,
        profile_number: int,
    ) -> dict[str, Any]:
        number = self._profile_number(profile_number, "motion")
        await self._require_profile_editable()
        async with self._profile_lock:
            document = self._motion_profile_document_for_edit()
            before = list(document["profiles"])
            document["profiles"] = [
                value
                for value in before
                if value.get("profile_number") != number
            ]
            if len(document["profiles"]) == len(before):
                raise KeyError(f"motion profile #{number} was not found")
            if document.get("default_profile_number") == number:
                document["default_profile_number"] = self._lowest_profile_number(
                    document["profiles"]
                )
            motion_profiles_from_document(document)
            self._write_json_atomic(self.motion_profiles_path, document)
        return {
            "deleted_profile_number": number,
            "motion_profiles": self._motion_profiles(),
        }

    async def set_development_motion_profile_default(
        self,
        profile_number: int,
    ) -> dict[str, Any]:
        number = self._profile_number(profile_number, "motion")
        await self._require_profile_editable()
        async with self._profile_lock:
            document = self._motion_profile_document_for_edit()
            if not any(
                value.get("profile_number") == number
                for value in document["profiles"]
            ):
                raise KeyError(f"motion profile #{number} was not found")
            document["default_profile_number"] = number
            motion_profiles_from_document(document)
            self._write_json_atomic(self.motion_profiles_path, document)
        return {
            "default_profile_number": number,
            "motion_profiles": self._motion_profiles(),
        }

    async def _require_profile_editable(self) -> None:
        async with self._state_lock:
            self._prune_development_locked(time.monotonic())
            if self._agent_workflow_active:
                raise RuntimeError("profiles cannot change during an Agent workflow")
            if self._active_development_session_id is not None:
                raise RuntimeError(
                    "profiles cannot change while a developer plan is staged"
                )

    async def _active_assembly_state(self) -> dict[str, Any]:
        if self.fabric is None:
            raise RuntimeError("Slicing requires the Fabric assembly-state stream")
        observation = await self.fabric.latest_optional("robot_arm.assembly_state")
        if not isinstance(observation, dict):
            raise RuntimeError("robot_arm.assembly_state is unavailable")
        state = observation.get("data")
        if (
            not isinstance(state, dict)
            or state.get("schema") != "midbrain.robot_assembly_state"
            or state.get("schema_version") != 1
        ):
            raise RuntimeError("robot_arm.assembly_state is unsupported")
        return copy.deepcopy(state)

    async def _active_effector_profile(self) -> dict[str, Any]:
        state = await self._active_assembly_state()
        mounted = state.get("mounted_effector")
        if not isinstance(mounted, dict):
            raise RuntimeError("the active assembly has no mounted-effector profile")
        blade_profiles_from_effector(mounted)
        return copy.deepcopy(mounted)

    async def _effective_slicing_effector_profile(self) -> dict[str, Any]:
        """Overlay only Slicing-owned profile data onto the active effector."""

        active = await self._active_effector_profile()
        source = self._source_effector_profile()
        self._require_same_effector_identity(source, active)
        extensions = active.setdefault("extensions", {})
        if not isinstance(extensions, dict):
            raise RuntimeError("the active mounted-effector extensions are invalid")
        extensions[BLADE_PROFILE_EXTENSION_ID] = copy.deepcopy(
            source["extensions"][BLADE_PROFILE_EXTENSION_ID]
        )
        blade_profiles_from_effector(active)
        return active

    async def _active_arm_joint_names(self) -> list[str]:
        state = await self._active_assembly_state()
        groups = state.get("resource_groups")
        groups = groups if isinstance(groups, list) else []
        arm = next(
            (
                value
                for value in groups
                if isinstance(value, dict) and value.get("group_id") == "arm"
            ),
            None,
        )
        names = arm.get("joint_names") if isinstance(arm, dict) else None
        if not isinstance(names, list) or len(names) != 6:
            raise RuntimeError("the active assembly has no six-joint arm group")
        result = [str(value).strip() for value in names]
        if any(not value for value in result) or len(set(result)) != 6:
            raise RuntimeError("the active assembly arm joint names are invalid")
        return result

    def _source_effector_profile(self) -> dict[str, Any]:
        try:
            profile = json.loads(
                self.effector_profile_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                "the selected mounted-effector source profile is unreadable"
            ) from exc
        if not isinstance(profile, dict):
            raise RuntimeError("the selected mounted-effector source profile is invalid")
        blade_profiles_from_effector(profile)
        return profile

    async def _blade_profile_status(self) -> dict[str, Any]:
        source_profile = self._source_effector_profile()
        active_profile = await self._active_effector_profile()
        self._require_same_effector_identity(source_profile, active_profile)
        source = blade_profiles_from_effector(source_profile)
        active = blade_profiles_from_effector(active_profile)
        return {
            "source": source,
            "active": active,
            "pending_workspace_restart": source != active,
            "agent_profile_selection_live": True,
            "source_profile_path": str(self.effector_profile_path),
        }

    def _motion_profile_document_for_edit(self) -> dict[str, Any]:
        path = (
            self.motion_profiles_path
            if self.motion_profiles_path.is_file()
            else self.motion_profiles_template_path
        )
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("the Slicing motion profiles are unreadable") from exc
        if not isinstance(document, dict):
            raise RuntimeError("the Slicing motion-profile document is invalid")
        motion_profiles_from_document(document)
        return copy.deepcopy(document)

    def _motion_profiles(self) -> dict[str, Any]:
        return motion_profiles_from_document(
            self._motion_profile_document_for_edit()
        )

    @staticmethod
    def _require_same_effector_identity(
        source: dict[str, Any],
        active: dict[str, Any],
    ) -> None:
        for name in ("profile_id", "profile_revision"):
            if source.get(name) != active.get(name):
                raise RuntimeError(
                    "the editable effector profile is not the active mounted effector"
                )

    @staticmethod
    def _profile_name(value: str | None, kind: str, number: int) -> str:
        name = str(value or "").strip() or f"{kind} profile #{number}"
        if len(name) > 120:
            raise ValueError("profile name must contain at most 120 characters")
        return name

    @staticmethod
    def _profile_number(value: Any, kind: str) -> int:
        if isinstance(value, bool):
            raise ValueError(f"{kind} profile number must be a positive integer")
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"{kind} profile number must be a positive integer"
            ) from exc
        if number <= 0:
            raise ValueError(f"{kind} profile number must be a positive integer")
        return number

    @staticmethod
    def _next_available_profile_number(values: Any) -> int:
        used = {int(value) for value in values}
        number = 1
        while number in used:
            number += 1
        return number

    @staticmethod
    def _lowest_profile_number(profiles: list[dict[str, Any]]) -> int | None:
        numbers = [
            int(value["profile_number"])
            for value in profiles
            if isinstance(value, dict) and "profile_number" in value
        ]
        return min(numbers) if numbers else None

    @staticmethod
    def _write_json_atomic(path: Path, document: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(document, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

    @staticmethod
    def _preview_rejection_message(preview: dict[str, Any]) -> str:
        status = str(preview.get("status") or "UNKNOWN")
        details: list[str] = []
        message = str(preview.get("message") or "").strip()
        if message:
            details.append(message)
        controller = preview.get("controller_preview")
        controller = controller if isinstance(controller, dict) else {}
        candidates = controller.get("candidate_evaluations")
        candidates = candidates if isinstance(candidates, list) else []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            reasons = candidate.get("planning_reasons")
            if isinstance(reasons, list):
                details.extend(str(value) for value in reasons if str(value).strip())
        suffix = "; ".join(dict.fromkeys(details))
        return (
            "Integrated did not produce a slicing alignment preview: "
            + status
            + (f"; {suffix}" if suffix else "")
        )

    @staticmethod
    def _orientation_distance_rad(first: Any, second: Any) -> float:
        first_rotation = SlicingHostAdapter._rpy_rotation(first)
        second_rotation = SlicingHostAdapter._rpy_rotation(second)
        relative = first_rotation @ second_rotation.T
        cosine = float(np.clip((np.trace(relative) - 1.0) * 0.5, -1.0, 1.0))
        return math.acos(cosine)

    @staticmethod
    def _rpy_rotation(value: Any) -> np.ndarray:
        roll, pitch, yaw = (float(component) for component in value)
        cr, sr = math.cos(roll), math.sin(roll)
        cp, sp = math.cos(pitch), math.sin(pitch)
        cy, sy = math.cos(yaw), math.sin(yaw)
        return np.asarray(
            [
                [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
                [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
                [-sp, cp * sr, cp * cr],
            ],
            dtype=float,
        )

    async def _measured_current_effector_arm_base(self) -> list[float]:
        observation = await self.integrated_motion.observation()
        controller = observation.get("controller")
        controller = controller if isinstance(controller, dict) else {}
        model_view = controller.get("model_view")
        model_view = model_view if isinstance(model_view, dict) else {}
        measured = model_view.get("measured_controlled_frame")
        measured = measured if isinstance(measured, dict) else {}
        position = measured.get("position_m")
        if not self._finite_vector3(position):
            raise RuntimeError(
                "Integrated has no finite measured controlled-effector position "
                "for relative point entry"
            )
        return [float(value) for value in position]

    @staticmethod
    def _finite_vector3(value: Any) -> bool:
        return (
            isinstance(value, (list, tuple))
            and len(value) == 3
            and all(
                not isinstance(component, bool)
                and isinstance(component, (int, float))
                and math.isfinite(float(component))
                for component in value
            )
        )

    async def _begin_development_stage(
        self,
        session_id: str,
        *,
        required_state: str,
        in_progress_state: str,
    ) -> _DevelopmentSession:
        async with self._state_lock:
            self._prune_development_locked(time.monotonic())
            session = self._require_session_locked(session_id)
            if self._active_development_session_id != session.session_id:
                raise RuntimeError("slicing developer session no longer owns execution")
            if session.state != required_state:
                raise RuntimeError(
                    f"slicing session requires {required_state}, not {session.state}"
                )
            session.state = in_progress_state
            session.updated_monotonic = time.monotonic()
            session.error = None
            return session

    async def _fail_development_session(
        self,
        session: _DevelopmentSession,
        *,
        state: str,
        error: Exception,
    ) -> None:
        async with self._state_lock:
            session.state = state
            session.error = str(error)
            session.updated_monotonic = time.monotonic()
            if self._active_development_session_id == session.session_id:
                self._active_development_session_id = None

    def _prune_development_locked(self, now: float) -> None:
        for session in self._development_sessions.values():
            if (
                session.state in _ACTIVE_DEVELOPMENT_STATES
                and now - session.updated_monotonic
                > self.development_session_ttl_s
            ):
                session.state = "EXPIRED_NO_CONTACT"
                session.error = "staged slicing session expired"
                if self._active_development_session_id == session.session_id:
                    self._active_development_session_id = None

    def _trim_sessions_locked(self) -> None:
        if len(self._development_sessions) <= 24:
            return
        ordered = sorted(
            self._development_sessions.values(),
            key=lambda value: value.updated_monotonic,
            reverse=True,
        )
        keep = {session.session_id for session in ordered[:24]}
        self._development_sessions = {
            key: value
            for key, value in self._development_sessions.items()
            if key in keep
        }

    def _require_session_locked(self, session_id: str) -> _DevelopmentSession:
        normalized = str(session_id or "").strip()
        session = self._development_sessions.get(normalized)
        if session is None:
            raise KeyError("slicing developer session was not found")
        return session

    @staticmethod
    def _completed_result(
        plan: SlicingPlan,
        alignment_result: dict[str, Any],
        contact_result: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "status": "SLICING_SEQUENCE_SUBMITTED_AND_RELAX_REQUESTED",
            "workflow_complete": True,
            "physical_motion_requested": True,
            "alignment": copy.deepcopy(alignment_result),
            "contact": copy.deepcopy(contact_result),
            "plan": plan.as_dict(),
            "task_success_assessed": False,
            "result_semantics": "COMMAND_HANDLING_ONLY",
        }


def build_host_adapter(
    *,
    skill_root: Path,
    manifest: dict[str, Any],
    services: Any,
) -> SlicingHostAdapter:
    installation = manifest.get("installation")
    installation = installation if isinstance(installation, dict) else {}
    motion_relative = str(
        installation.get("motion_profile_config")
        or "config/motion_profiles.json"
    )
    motion_template_relative = str(
        installation.get("motion_profile_template")
        or "config_templates/motion_profiles.default.json"
    )
    assembly_relative = str(
        installation.get("assembly_selection")
        or "../../config/robot_assemblies/primary_manipulator.json"
    )
    skill_root = skill_root.resolve()
    motion_path = (skill_root / motion_relative).resolve()
    motion_template_path = (skill_root / motion_template_relative).resolve()
    if skill_root not in motion_path.parents:
        raise RuntimeError("Slicing motion-profile configuration escaped the Skill")
    if skill_root not in motion_template_path.parents:
        raise RuntimeError("Slicing motion-profile template escaped the Skill")
    effector_profile_path = _selected_effector_profile_path(
        skill_root,
        assembly_relative,
    )
    return SlicingHostAdapter(
        manager=services.manager,
        fabric=getattr(services, "fabric", None),
        integrated_motion=getattr(services, "integrated_motion", None),
        contact_provider_url=str(
            getattr(
                services,
                "contact_provider_url",
                "http://127.0.0.1:8794",
            )
        ),
        effector_profile_path=effector_profile_path,
        motion_profiles_path=motion_path,
        motion_profiles_template_path=motion_template_path,
    )


def _selected_effector_profile_path(
    skill_root: Path,
    assembly_relative: str,
) -> Path:
    workspace_root = skill_root.parents[1]
    selection_path = (skill_root / assembly_relative).resolve()
    if selection_path != workspace_root and workspace_root not in selection_path.parents:
        raise RuntimeError("robot assembly selection escaped the workspace")
    try:
        selection = json.loads(selection_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("robot assembly selection is unreadable") from exc
    arm_provider = selection.get("arm_provider")
    arm_provider = arm_provider if isinstance(arm_provider, dict) else {}
    provider_root = (
        workspace_root / str(arm_provider.get("provider_root") or "")
    ).resolve()
    if provider_root != workspace_root and workspace_root not in provider_root.parents:
        raise RuntimeError("selected arm Provider root escaped the workspace")
    profiles = selection.get("profiles")
    profiles = profiles if isinstance(profiles, dict) else {}
    mounted = profiles.get("mounted_effector")
    mounted = mounted if isinstance(mounted, dict) else {}
    relative_path = str(mounted.get("relative_path") or "").strip()
    if not relative_path:
        raise RuntimeError("robot assembly selection has no mounted-effector path")
    profile_path = (provider_root / relative_path).resolve()
    if profile_path != provider_root and provider_root not in profile_path.parents:
        raise RuntimeError("selected mounted-effector profile escaped its Provider")
    if not profile_path.is_file():
        raise RuntimeError("selected mounted-effector profile is unavailable")
    return profile_path
