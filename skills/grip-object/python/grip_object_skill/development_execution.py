from __future__ import annotations

from pathlib import Path
from threading import RLock
from typing import Any
import json
import time
import uuid

from grip_work_runtime import (
    ContactStagedRuntime,
    GripRuntime,
    contact_step,
    failed_grip_result,
)
from grip_work_runtime.point_entry import (
    RELATIVE_WORLD_POINT_MODE,
    normalize_point_mode,
    resolve_approach_point,
)
from grip_work_runtime.development_execution import (
    IntegratedDevelopmentRuntime,
    ManagerDevelopmentRuntime,
    active_motion_calibration,
    assert_provider_identity,
    development_session,
    provider_identity,
    public_session,
)

from .skill import build_plan


SKILL_ID = "grip.grip_object"
CONTACT_PROVIDER_ID = "robot_arm.primary.contact"
GRIP_PROVIDER_ID = "robot_arm.primary.grip"


def _load(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _calibration_identity(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "activation_id": value.get("activation_id"),
        "calibration_revision": value.get("calibration_revision"),
        "translation_refinement_revision": value.get(
            "translation_refinement_revision"
        ),
    }


def _require_grip_ready(state: dict[str, Any], phase: str) -> dict[str, Any]:
    thermal = state.get("thermal")
    if isinstance(thermal, dict) and thermal.get("ready_for_new_grip") is True:
        return thermal
    thermal = thermal if isinstance(thermal, dict) else {}
    hot = thermal.get("hot_joint_indices") or []
    unavailable = thermal.get("unavailable_joint_indices") or []
    age = thermal.get("feedback_age_ms")
    retry = float(thermal.get("retry_after_s", 60.0))
    raise RuntimeError(
        f"grip is not ready during {phase}; hot joints={hot}, unavailable joints="
        f"{unavailable}, feedback age ms={age}; retry recommendation={retry:.0f} seconds"
    )


class ScrapGripDevelopmentExecution:
    """Attended Integrated alignment followed by Contact and Grip stages."""

    stage_definitions = [
        {
            "stage_number": 1,
            "name": "Integrated alignment with concurrent gripper opening",
            "description": (
                "At the same time, open the gripper to the functional -180 degree "
                "threshold through a 50 Hz MIT transition and execute the Slicing-style "
                "rotation-only collision preview at the measured current position. "
                "Require approach-ready gripper state, finish the arm in verified FLOAT, "
                "and hand off Integrated WARM to Contact HOT."
            ),
        },
        {
            "stage_number": 2,
            "name": "Contact approach and table-inward move",
            "description": (
                "Move to the frozen absolute approach-begin point plus the table-inward "
                "offset through Contact while holding POS_TOR."
            ),
        },
        {
            "stage_number": 3,
            "name": "Contact insertion move",
            "description": "Move the frozen insertion distance while holding POS_TOR.",
        },
        {
            "stage_number": 4,
            "name": "Grip and confirm carry",
            "description": (
                "Close toward the frozen normal-object endpoint, infer contact, and "
                "bind carry. Without stable contact, open and float the gripper, "
                "relax Contact, and finish with a failed-grip result."
            ),
        },
    ]

    def __init__(
        self,
        *,
        skill_root: Path,
        manager_url: str,
        contact_url: str,
        grip_url: str,
        integrated_url: str,
        authorization_url: str,
    ) -> None:
        self.skill_root = skill_root.resolve()
        workspace = self.skill_root.parents[1]
        assembly = _load(
            workspace / "config/robot_assemblies/primary_manipulator.json"
        )
        provider_root = workspace / str(assembly["arm_provider"]["provider_root"])
        self.effector_path = (
            provider_root
            / "profiles/effectors/rebot_b601_dm_bare_gripper_grip_control.v1.json"
        ).resolve()
        self.manager = ManagerDevelopmentRuntime(manager_url)
        self.manager_url = manager_url.rstrip("/")
        self.contact_url = contact_url.rstrip("/")
        self.grip_url = grip_url.rstrip("/")
        self.integrated_url = integrated_url.rstrip("/")
        self.authorization_url = authorization_url.rstrip("/")
        self.session: dict[str, Any] | None = None
        self.lock = RLock()

    def observation(self) -> dict[str, Any]:
        with self.lock:
            return {
                "available": True,
                "controller": "Integrated then Contact",
                "collision_planning": "INTEGRATED_ALIGNMENT_ONLY",
                "stage_definitions": self.stage_definitions,
                "session": public_session(self.session),
                "execution_policy": (
                    "Developer-only attended physical execution matching Slicing's controller "
                    "division: Grip independently opens joint 7 through a 50 Hz MIT transition "
                    "while Integrated performs one rotation-only collision-checked arm move and "
                    "ends in verified FLOAT; Integrated is confirmed WARM with no arm-group Basic "
                    "lease before Contact becomes HOT. Contact then owns the absolute engage target "
                    "and relative insertion. No language-Agent reasoning is invoked."
                ),
            }

    def profiles_locked(self) -> bool:
        with self.lock:
            return self.session is not None and self.session.get("status") not in {
                "COMPLETED",
                "CANCELLED",
                "GRIP_FAILED",
            }

    def prepare(self, prepared_plan: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            if self.profiles_locked():
                raise RuntimeError("finish or cancel the active development session first")
            self.manager.assert_motion_allowed()
            self.manager.set_hot(GRIP_PROVIDER_ID)
            self.manager.set_hot("robot_arm.primary.integrated")
            grip = GripRuntime(
                self.grip_url,
                signing_secret_env="MIDBRAIN_GRIP_OBJECT_SECRET",
            )
            grip_state = grip.state()
            if isinstance(grip_state.get("carry"), dict):
                raise RuntimeError(
                    "Scrap Grip cannot prepare while an object is already confirmed carried"
                )
            _require_grip_ready(grip_state, "stage 0 preparation")
            contact = ContactStagedRuntime(
                self.contact_url,
                self.manager_url,
                signing_secret_env="MIDBRAIN_CONTACT_GRIP_OBJECT_SECRET",
            )
            integrated = IntegratedDevelopmentRuntime(
                self.integrated_url,
                self.authorization_url,
                requester_id=f"{SKILL_ID}.development",
                operation_label="Scrap Grip development",
            )
            integrated_state = integrated.state()
            activation = active_motion_calibration(
                self.manager.workcell_calibrations()
            )
            point_mode = normalize_point_mode(prepared_plan.get("point_mode"))
            current_effector_base_m = None
            if point_mode == RELATIVE_WORLD_POINT_MODE:
                current_effector_base_m = (
                    integrated.measured_controlled_frame_position(
                        integrated_state
                    )
                )
            point_resolution = resolve_approach_point(
                prepared_plan["approach_begin_point_world_m"],
                point_mode=point_mode,
                world_from_base=activation["transforms"]["world_from_base"],
                current_effector_base_m=current_effector_base_m,
            )
            vector_number = int(prepared_plan["gripper_vector_profile_number"])
            vector_document = _load(
                self.skill_root / "config/gripper_vector_profiles.json"
            )
            selected_vector = next(
                (
                    item
                    for item in vector_document["profiles"]
                    if int(item["profile_number"]) == vector_number
                ),
                None,
            )
            if not isinstance(selected_vector, dict):
                raise RuntimeError("selected gripper vector profile is unavailable")
            typed_vectors = {
                "profile_number": vector_number,
                "name": str(selected_vector["name"]),
                **prepared_plan["gripper_vectors"],
            }
            arguments = {
                **prepared_plan["object_vectors"],
                "approach_begin_point_world_m": point_resolution[
                    "resolved_approach_begin_point_world_m"
                ],
                "object_binding": prepared_plan["object_binding"],
            }
            plan = build_plan(
                arguments,
                effector_profile=_load(self.effector_path),
                gripper_vector_profile=typed_vectors,
                motion_profile=prepared_plan["motion"],
                world_from_base=activation["transforms"]["world_from_base"],
            )
            plan["construction"] = (
                "CONCURRENT_GRIPPER_MIT_OPEN_AND_INTEGRATED_ROTATION_THEN_CONTACT_ABSOLUTE_ENGAGE_"
                "THEN_RELATIVE_INSERT_THEN_GRIP"
            )
            integrated_preview = integrated.preview_rotation(
                target_rpy_rad=plan["target_orientation_rpy_rad"],
                calibration_binding=_calibration_identity(activation),
            )
            carry_id = str(uuid.uuid4())
            attachment_revision = str(uuid.uuid4())
            session = development_session(
                session_id=str(uuid.uuid4()),
                stage_definitions=self.stage_definitions,
                frozen_plan={
                    **prepared_plan,
                    "point_resolution": point_resolution,
                    "resolved_contact_plan": plan,
                },
            )
            session.update(
                {
                    "controller": "Integrated then Contact",
                    "collision_planning": "INTEGRATED_ALIGNMENT_ONLY",
                    "calibration": _calibration_identity(activation),
                    "point_resolution": point_resolution,
                    "integrated_controller_identity": integrated_preview[
                        "controller_identity"
                    ],
                    "integrated_preview": integrated_preview["preview"],
                    "integrated_rotation": {
                        "policy": "SLICING_STYLE_ROTATION_ONLY",
                        "measured_start_position_m": integrated_preview[
                            "measured_start_position_m"
                        ],
                        "frozen_target_rpy_rad": plan[
                            "target_orientation_rpy_rad"
                        ],
                    },
                    "grip_provider_identity": provider_identity(grip_state),
                    "carry_id": carry_id,
                    "attachment_revision": attachment_revision,
                    "_contact": contact,
                    "_integrated": integrated,
                    "_integrated_prepared": integrated_preview,
                    "_grip": grip,
                    "_plan": plan,
                }
            )
            session["next_stage_deadline_at_us"] = integrated_preview[
                "preview"
            ]["preview_contract"]["expires_at_us"]
            self.session = session
            return public_session(session) or {}

    def _validate_session(self, session_id: str, stage_number: int) -> dict[str, Any]:
        if self.session is None or self.session.get("session_id") != session_id:
            raise ValueError("development session is unavailable or stale")
        if self.session.get("status") not in {"PREPARED", "AWAITING_STAGE"}:
            raise RuntimeError(
                f"development session is {self.session.get('status')}; cancel and prepare again"
            )
        expected = int(self.session["next_stage_number"])
        if int(stage_number) != expected:
            raise RuntimeError(
                f"stage {stage_number} is unavailable; the next stage is {expected}"
            )
        self.manager.assert_motion_allowed()
        current_activation = active_motion_calibration(
            self.manager.workcell_calibrations()
        )
        if _calibration_identity(current_activation) != self.session["calibration"]:
            raise RuntimeError("workcell calibration changed; cancel and prepare again")
        grip: GripRuntime = self.session["_grip"]
        if stage_number == 1:
            integrated: IntegratedDevelopmentRuntime = self.session[
                "_integrated"
            ]
            if integrated.identity(integrated.state()) != self.session[
                "integrated_controller_identity"
            ]:
                raise RuntimeError(
                    "Integrated identity changed after preview; prepare again"
                )
        else:
            contact: ContactStagedRuntime = self.session["_contact"]
            expected_contact = self.session.get("contact_provider_identity")
            if not isinstance(expected_contact, dict):
                raise RuntimeError(
                    "Contact is not prepared; execute Integrated stage 1 first"
                )
            assert_provider_identity(expected_contact, contact.state())
        assert_provider_identity(self.session["grip_provider_identity"], grip.state())
        return self.session

    def execute_stage(
        self,
        session_id: str,
        stage_number: int,
        *,
        physical_acknowledged: bool,
    ) -> dict[str, Any]:
        with self.lock:
            if not physical_acknowledged:
                raise PermissionError(
                    "explicit physical-stage acknowledgement is required"
                )
            session = self._validate_session(session_id, stage_number)
            contact: ContactStagedRuntime = session["_contact"]
            grip: GripRuntime = session["_grip"]
            plan = session["_plan"]
            session["status"] = "EXECUTING_STAGE"
            session["error"] = None
            opening_execution_id: str | None = None
            grip_execution_id: str | None = None
            grip_cleanup_done = False
            try:
                if stage_number == 1:
                    integrated: IntegratedDevelopmentRuntime = session[
                        "_integrated"
                    ]
                    grip_state = grip.state()
                    _require_grip_ready(grip_state, "stage 1 execution")
                    opening_execution_id = str(uuid.uuid4())
                    opening = grip.command(
                        skill_id=SKILL_ID,
                        execution_id=opening_execution_id,
                        operation="SET_MIT_POSITION",
                        intent="OPEN",
                        position_rad=plan["approach_open"]["position_rad"],
                        duration_s=plan["approach_open"]["duration_s"],
                        kp=plan["approach_open"]["kp"],
                        kd=plan["approach_open"]["kd"],
                    )
                    alignment = integrated.execute(session["_integrated_prepared"])
                    handoff = integrated.handoff_to_contact(
                        self.manager,
                        CONTACT_PROVIDER_ID,
                    )
                    opened = grip.wait_for(
                        lambda state: state.get("ready_for_approach") is True,
                        timeout_s=7.0,
                        description="thermally ready, functionally open gripper MIT hold",
                    )
                    session["contact_provider_identity"] = provider_identity(
                        contact.state()
                    )
                    result = {
                        "gripper_opening": opening,
                        "gripper_approach_ready": opened,
                        "alignment": alignment,
                        "controller_handoff": handoff,
                        "rotation_only": True,
                        "gripper_opened_concurrently": True,
                    }
                elif stage_number == 2:
                    engage_position = [
                        plan["approach_begin_point_base_m"][index]
                        + plan["table_delta_base_m"][index]
                        for index in range(3)
                    ]
                    contact.begin(
                        skill_id=SKILL_ID,
                        steps=[
                            contact_step(
                                position_m=engage_position,
                                orientation_xyzw=plan["orientation_xyzw"],
                                position_mode="ABSOLUTE_ROOT",
                                delay_after_accept_s=plan["stage_waits_s"][
                                    "lower"
                                ],
                                next_command_timeout_s=max(
                                    6.0,
                                    plan["stage_waits_s"]["lower"] + 1.0,
                                ),
                            ),
                            contact_step(
                                position_m=plan["insertion_delta_base_m"],
                                orientation_xyzw=plan["orientation_xyzw"],
                                delay_after_accept_s=plan["stage_waits_s"][
                                    "scrap"
                                ],
                                next_command_timeout_s=max(
                                    20.0,
                                    plan["stage_waits_s"]["scrap"]
                                    + plan["grip"]["contact_timeout_s"]
                                    + plan["stage_waits_s"]["grip"]
                                    + 7.0,
                                ),
                            ),
                        ],
                        carry_id=session["carry_id"],
                        attachment_revision=session["attachment_revision"],
                        behavior="PREPARE",
                    )
                    result = contact.move(0)
                elif stage_number == 3:
                    result = contact.move(1)
                else:
                    grip_execution_id = str(uuid.uuid4())
                    close = grip.command(
                        skill_id=SKILL_ID,
                        execution_id=grip_execution_id,
                        operation="SET_POSITION_EFFORT",
                        intent="GRIP",
                        position_rad=plan["grip"]["position_rad"],
                        velocity_limit_rad_s=plan["grip"]["velocity_limit_rad_s"],
                        torque_limit_nm=plan["grip"]["torque_limit_nm"],
                    )
                    try:
                        inferred = grip.wait_for(
                            lambda state: state.get("contact_inferred") is True,
                            timeout_s=plan["grip"]["contact_timeout_s"],
                            description="stable gripper contact inference",
                        )
                        time.sleep(
                            plan.get("stage_waits_s", {}).get("grip", 1.5)
                        )
                    except TimeoutError as exc:
                        cleanup = self._cleanup_unconfirmed_grip(
                            session,
                            execution_id=grip_execution_id,
                            reason=(
                                "scrap-grip development found no stable contact "
                                "before the close endpoint"
                            ),
                        )
                        grip_cleanup_done = True
                        result = failed_grip_result(
                            target_position_rad=plan["grip"]["position_rad"],
                            failure=exc,
                            cleanup=cleanup,
                        )
                        result["close"] = close
                        inferred = None
                    if inferred is None:
                        contact_confirmation = None
                        grip_confirmation = None
                    else:
                        contact_confirmation = contact.confirm_staged_carry()
                        grip_confirmation = grip.command(
                            skill_id=SKILL_ID,
                            execution_id=grip_execution_id,
                            operation="CONFIRM_CARRY",
                            carry_id=session["carry_id"],
                            attachment_revision=session["attachment_revision"],
                            attachment=plan["attachment"],
                        )
                        contact.close("scrap-grip development carry confirmed")
                        result = {
                            "status": "CARRYING_POSITION_EFFORT_LIMITED",
                            "grip_confirmed": True,
                            "close": close,
                            "contact_inference": inferred,
                            "contact_carry_confirmation": contact_confirmation,
                            "grip_carry_confirmation": grip_confirmation,
                            "all_arm_joints_position_effort_limited": True,
                        }
                session["updated_at_us"] = time.time_ns() // 1000
                session["stage_results"].append(
                    {"stage_number": stage_number, "result": result}
                )
                if result.get("grip_confirmed") is False:
                    session["status"] = "GRIP_FAILED"
                    session["next_stage_number"] = None
                    session["next_stage_deadline_at_us"] = None
                elif stage_number == len(self.stage_definitions):
                    session["status"] = "COMPLETED"
                    session["next_stage_number"] = None
                    session["next_stage_deadline_at_us"] = None
                else:
                    session["status"] = "AWAITING_STAGE"
                    session["next_stage_number"] = stage_number + 1
                    session["next_stage_deadline_at_us"] = result.get(
                        "next_command_deadline_at_us"
                    )
            except Exception as exc:
                if stage_number == 1 and opening_execution_id is not None:
                    try:
                        grip.command(
                            skill_id=SKILL_ID,
                            execution_id=opening_execution_id,
                            operation="ENTER_MIT_FLOAT",
                            delta_time_s=plan["release"]["mit_delta_time_s"],
                        )
                    except Exception:
                        pass
                if (
                    stage_number == 4
                    and grip_execution_id is not None
                    and not grip_cleanup_done
                ):
                    self._cleanup_unconfirmed_grip(
                        session,
                        execution_id=grip_execution_id,
                        reason="scrap-grip development failed before carry confirmation",
                    )
                session["status"] = "STAGE_FAILED"
                session["updated_at_us"] = time.time_ns() // 1000
                session["error"] = {
                    "stage_number": stage_number,
                    "type": type(exc).__name__,
                    "message": str(exc),
                }
                raise RuntimeError(
                    f"Scrap Grip stage {stage_number} failed: {exc}"
                ) from exc
            return public_session(session) or {}

    def _cleanup_unconfirmed_grip(
        self,
        session: dict[str, Any],
        *,
        execution_id: str,
        reason: str,
    ) -> dict[str, Any]:
        grip: GripRuntime = session["_grip"]
        contact: ContactStagedRuntime = session["_contact"]
        release = session["_plan"]["release"]
        cleanup: dict[str, Any] = {"errors": []}
        try:
            cleanup["gripper_release"] = grip.open_and_float(
                skill_id=SKILL_ID,
                execution_id=execution_id,
                position_rad=release["position_rad"],
                velocity_limit_rad_s=release["velocity_limit_rad_s"],
                torque_limit_nm=release["torque_limit_nm"],
                position_tolerance_rad=release["position_tolerance_rad"],
                open_timeout_s=7.0,
                mit_delta_time_s=release["mit_delta_time_s"],
            )
        except Exception as exc:
            cleanup["errors"].append(f"gripper release: {exc}")
        try:
            if contact.session_id is not None:
                cleanup["contact_relax"] = contact.relax_staged(reason)
            else:
                contact.close(reason)
                cleanup["contact_relax"] = {"already_closed": True}
        except Exception as exc:
            cleanup["errors"].append(f"Contact relax: {exc}")
        return cleanup

    def _open_and_float(self, session: dict[str, Any]) -> None:
        grip: GripRuntime = session["_grip"]
        plan = session["_plan"]
        execution_id = str(uuid.uuid4())
        release = plan["release"]
        grip.open_and_float(
            skill_id=SKILL_ID,
            execution_id=execution_id,
            position_rad=release["position_rad"],
            velocity_limit_rad_s=release["velocity_limit_rad_s"],
            torque_limit_nm=release["torque_limit_nm"],
            position_tolerance_rad=release["position_tolerance_rad"],
            open_timeout_s=7.0,
            mit_delta_time_s=release["mit_delta_time_s"],
        )

    def cancel(self, session_id: str) -> dict[str, Any]:
        with self.lock:
            if self.session is None or self.session.get("session_id") != session_id:
                raise ValueError("development session is unavailable or stale")
            if self.session.get("status") in {"COMPLETED", "GRIP_FAILED"}:
                raise RuntimeError(
                    "a terminal carry result cannot be cancelled from this page"
                )
            session = self.session
            errors: list[str] = []
            contact_started = session["_contact"].session_id is not None
            if contact_started or (
                session.get("status") == "STAGE_FAILED"
                and int(session.get("next_stage_number") or 0) >= 4
            ):
                try:
                    self._open_and_float(session)
                except Exception as exc:
                    errors.append(f"gripper cleanup: {exc}")
            contact: ContactStagedRuntime = session["_contact"]
            if contact.session_id is not None:
                try:
                    contact.relax_staged("scrap-grip development cancelled")
                except Exception as exc:
                    errors.append(f"Contact relax: {exc}")
            else:
                contact.close("scrap-grip development cancelled before Contact start")
            session["status"] = "CANCELLED" if not errors else "CANCEL_FAILED"
            session["updated_at_us"] = time.time_ns() // 1000
            session["next_stage_number"] = None
            session["next_stage_deadline_at_us"] = None
            session["error"] = errors or None
            if errors:
                raise RuntimeError("; ".join(errors))
            return public_session(session) or {}
