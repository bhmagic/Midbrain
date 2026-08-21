from __future__ import annotations

from pathlib import Path
from threading import RLock
from typing import Any
import json
import math
import time
import uuid

from grip_work_runtime import (
    ContactStagedRuntime,
    GripRuntime,
    contact_step,
    two_vector_orientation,
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


SKILL_ID = "grip.lay_flat"
CONTACT_PROVIDER_ID = "robot_arm.primary.contact"
GRIP_PROVIDER_ID = "robot_arm.primary.grip"


def _load(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _matvec(matrix: list[list[float]], vector: list[float]) -> list[float]:
    return [
        sum(matrix[row][column] * vector[column] for column in range(3))
        for row in range(3)
    ]


def _quaternion_rpy(value: list[float]) -> list[float]:
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError("orientation quaternion must contain four values")
    x, y, z, w = (float(component) for component in value)
    norm = math.sqrt(x * x + y * y + z * z + w * w)
    if not math.isfinite(norm) or norm < 1e-8:
        raise ValueError("orientation quaternion must be finite and nonzero")
    x, y, z, w = x / norm, y / norm, z / norm, w / norm
    return [
        math.atan2(2.0 * (w * x + y * z), 1.0 - 2.0 * (x * x + y * y)),
        math.asin(max(-1.0, min(1.0, 2.0 * (w * y - z * x)))),
        math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z)),
    ]


def _calibration_identity(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "activation_id": value.get("activation_id"),
        "calibration_revision": value.get("calibration_revision"),
        "translation_refinement_revision": value.get(
            "translation_refinement_revision"
        ),
    }


class LayFlatDevelopmentExecution:
    """Attended Slicing-style Integrated-to-Contact Lay Flat execution."""

    stage_definitions = [
        {
            "stage_number": 1,
            "name": "Integrated orientation alignment",
            "description": (
                "Execute the Slicing-style rotation-only collision preview at the "
                "measured current position, finish in verified FLOAT, and hand off "
                "Integrated WARM to Contact HOT."
            ),
        },
        {
            "stage_number": 2,
            "name": "Contact place carried object",
            "description": (
                "Move to the frozen absolute approach target plus table-inward and "
                "negative-insertion offsets through Contact POS_TOR."
            ),
        },
        {
            "stage_number": 3,
            "name": "Release gripper",
            "description": (
                "Release the carried object, verify measured-open, then put only the "
                "gripper joint into MIT float. Arm joints remain Contact POS_TOR."
            ),
        },
        {
            "stage_number": 4,
            "name": "Contact retreat and relax",
            "description": (
                "Retreat against insertion through Contact, then relax the arm after "
                "the finite lay-flat sequence completes."
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
                "collision_planning": "INTEGRATED_ALIGNMENT_ONLY_CARRY_EXCEPTION",
                "stage_definitions": self.stage_definitions,
                "session": public_session(self.session),
                "execution_policy": (
                    "Developer-only attended execution matching Slicing's controller division. "
                    "Integrated performs one rotation-only collision-checked move, ends in "
                    "verified FLOAT, is confirmed WARM without a Basic lease, and then Contact "
                    "owns absolute placement and relative retreat. Because Lay Flat starts with "
                    "a confirmed carry, Stage 1 is an explicit attended-development exception "
                    "to the normal uninterrupted Contact POS_TOR carry rule. The regular Skill "
                    "uses the same declared Integrated-then-Contact controller sequence."
                ),
            }

    def profiles_locked(self) -> bool:
        with self.lock:
            return self.session is not None and self.session.get("status") not in {
                "COMPLETED",
                "CANCELLED",
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
                signing_secret_env="MIDBRAIN_GRIP_LAY_FLAT_SECRET",
            )
            grip_state = grip.state()
            carry = grip_state.get("carry")
            if not isinstance(carry, dict):
                raise RuntimeError("lay-flat requires a confirmed carried object")
            contact = ContactStagedRuntime(
                self.contact_url,
                self.manager_url,
                signing_secret_env="MIDBRAIN_CONTACT_LAY_FLAT_SECRET",
            )
            integrated = IntegratedDevelopmentRuntime(
                self.integrated_url,
                self.authorization_url,
                requester_id=f"{SKILL_ID}.development",
                operation_label="Lay Flat development",
            )
            integrated_state = integrated.state()
            activation = active_motion_calibration(
                self.manager.workcell_calibrations()
            )
            effector = _load(self.effector_path)
            if effector.get("schema") != "midbrain.grip_effector_control_profile":
                raise RuntimeError("active effector has no compatible grip-control profile")
            orientation = two_vector_orientation(
                effector_first=prepared_plan["gripper_vectors"][
                    "table_inward_direction_effector"
                ],
                effector_second=prepared_plan["gripper_vectors"][
                    "insertion_direction_effector"
                ],
                world_first=prepared_plan["object_vectors"][
                    "table_inward_direction_world"
                ],
                world_second=prepared_plan["object_vectors"][
                    "insertion_direction_world"
                ],
                world_from_base_quaternion=activation["transforms"][
                    "world_from_base"
                ]["rotation_xyzw"],
            )
            motion = prepared_plan["motion"]
            table_distance = float(motion["table_inward_distance_m"])
            negative_insertion = float(motion["negative_insertion_distance_m"])
            retreat_distance = float(motion["retreat_distance_m"])
            if not all(
                math.isfinite(value) and 0.0 < value <= 0.25
                for value in (
                    table_distance,
                    negative_insertion,
                    retreat_distance,
                )
            ):
                raise ValueError("lay-flat distances must be finite and in (0, 0.25] m")
            combined_world = [
                orientation["world_first"][index] * table_distance
                - orientation["world_second"][index] * negative_insertion
                for index in range(3)
            ]
            retreat_world = [
                -value * retreat_distance for value in orientation["world_second"]
            ]
            world_from_base = activation["transforms"]["world_from_base"]
            combined_delta_base = _matvec(
                orientation["base_world_rotation"], combined_world
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
                world_from_base=world_from_base,
                current_effector_base_m=current_effector_base_m,
            )
            placement_position_base_m = [
                point_resolution["resolved_approach_begin_point_base_m"][index]
                + combined_delta_base[index]
                for index in range(3)
            ]
            target_rpy_rad = _quaternion_rpy(
                orientation["orientation_arm_base_xyzw"]
            )
            resolved_plan = {
                "orientation_xyzw": orientation["orientation_arm_base_xyzw"],
                "target_orientation_rpy_rad": target_rpy_rad,
                "combined_delta_base_m": combined_delta_base,
                "approach_begin_point_base_m": point_resolution[
                    "resolved_approach_begin_point_base_m"
                ],
                "placement_position_base_m": placement_position_base_m,
                "placement_position_mode": "ABSOLUTE_ROOT",
                "retreat_delta_base_m": _matvec(
                    orientation["base_world_rotation"], retreat_world
                ),
                "motion": motion,
                "construction": (
                    "INTEGRATED_ROTATION_ONLY_THEN_CONTACT_ABSOLUTE_PLACE_"
                    "THEN_RELEASE_THEN_RELATIVE_RETREAT"
                ),
            }
            integrated_preview = integrated.preview_rotation(
                target_rpy_rad=target_rpy_rad,
                calibration_binding=_calibration_identity(activation),
            )
            session = development_session(
                session_id=str(uuid.uuid4()),
                stage_definitions=self.stage_definitions,
                frozen_plan={
                    **prepared_plan,
                    "point_resolution": point_resolution,
                    "resolved_contact_plan": resolved_plan,
                },
            )
            session.update(
                {
                    "controller": "Integrated then Contact",
                    "collision_planning": (
                        "INTEGRATED_ALIGNMENT_ONLY_CARRY_EXCEPTION"
                    ),
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
                        "frozen_target_rpy_rad": target_rpy_rad,
                        "confirmed_carry_development_exception": True,
                    },
                    "grip_provider_identity": provider_identity(grip_state),
                    "carry_id": str(carry["carry_id"]),
                    "attachment_revision": str(carry["attachment_revision"]),
                    "_contact": contact,
                    "_integrated": integrated,
                    "_integrated_prepared": integrated_preview,
                    "_grip": grip,
                    "_plan": resolved_plan,
                    "_release_started": False,
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
                f"development session is {self.session.get('status')}; resolve it before continuing"
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
            try:
                if stage_number == 1:
                    integrated: IntegratedDevelopmentRuntime = session[
                        "_integrated"
                    ]
                    alignment = integrated.execute(
                        session["_integrated_prepared"]
                    )
                    handoff = integrated.handoff_to_contact(
                        self.manager,
                        CONTACT_PROVIDER_ID,
                    )
                    session["contact_provider_identity"] = provider_identity(
                        contact.state()
                    )
                    result = {
                        "alignment": alignment,
                        "controller_handoff": handoff,
                        "rotation_only": True,
                        "confirmed_carry_development_exception": True,
                    }
                elif stage_number == 2:
                    contact.begin(
                        skill_id=SKILL_ID,
                        steps=[
                            contact_step(
                                position_m=plan["placement_position_base_m"],
                                orientation_xyzw=plan["orientation_xyzw"],
                                position_mode=plan["placement_position_mode"],
                                next_command_timeout_s=60.0,
                            ),
                            contact_step(
                                position_m=plan["retreat_delta_base_m"],
                                orientation_xyzw=plan["orientation_xyzw"],
                                next_command_timeout_s=60.0,
                            ),
                        ],
                        carry_id=session["carry_id"],
                        attachment_revision=session["attachment_revision"],
                        behavior="CONTINUE",
                    )
                    result = contact.move(0)
                elif stage_number == 3:
                    session["_release_started"] = True
                    execution_id = str(uuid.uuid4())
                    release = grip.command(
                        skill_id=SKILL_ID,
                        execution_id=execution_id,
                        operation="RELEASE_OBJECT",
                        carry_id=session["carry_id"],
                    )
                    target = float(release["target_position_rad"])
                    opened = grip.wait_for(
                        lambda state: state.get("gripper_position_rad") is not None
                        and abs(float(state["gripper_position_rad"]) - target) <= 0.08,
                        timeout_s=float(plan["motion"]["open_timeout_s"]),
                        description="measured open gripper",
                    )
                    float_result = grip.command(
                        skill_id=SKILL_ID,
                        execution_id=execution_id,
                        operation="ENTER_MIT_FLOAT",
                        delta_time_s=float(plan["motion"]["mit_delta_time_s"]),
                    )
                    floated = grip.wait_for(
                        lambda state: state.get("state") == "MIT_FLOAT",
                        timeout_s=float(plan["motion"]["mit_delta_time_s"]) + 2.0,
                        description="gripper MIT float",
                    )
                    result = {
                        "release": release,
                        "opened": opened,
                        "mit_float": float_result,
                        "float_state": floated,
                        "arm_joints_remain_position_effort_limited": True,
                        "next_command_deadline_at_us": session.get(
                            "next_stage_deadline_at_us"
                        ),
                    }
                else:
                    retreat = contact.move(1)
                    relaxed = contact.relax_staged(
                        "lay-flat development release and retreat complete"
                    )
                    result = {"retreat": retreat, "contact_relax": relaxed}
                session["stage_results"].append(
                    {"stage_number": stage_number, "result": result}
                )
                session["updated_at_us"] = time.time_ns() // 1000
                if stage_number == len(self.stage_definitions):
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
                session["status"] = "STAGE_FAILED"
                session["updated_at_us"] = time.time_ns() // 1000
                session["error"] = {
                    "stage_number": stage_number,
                    "type": type(exc).__name__,
                    "message": str(exc),
                }
                raise RuntimeError(
                    f"Lay Flat stage {stage_number} failed: {exc}"
                ) from exc
            return public_session(session) or {}

    def cancel(self, session_id: str) -> dict[str, Any]:
        with self.lock:
            if self.session is None or self.session.get("session_id") != session_id:
                raise ValueError("development session is unavailable or stale")
            session = self.session
            if session.get("status") == "COMPLETED":
                raise RuntimeError("the lay-flat development session is already complete")
            if session.get("_release_started") is True:
                raise RuntimeError(
                    "release has started; cancellation is unsafe—complete the Contact retreat"
                )
            contact: ContactStagedRuntime = session["_contact"]
            stage_one_completed = any(
                item.get("stage_number") == 1
                for item in session.get("stage_results", [])
                if isinstance(item, dict)
            )
            recovered_contact_hold = None
            if stage_one_completed and contact.session_id is None:
                contact_state = contact.state()
                measured_pose = contact_state.get("measured_acting_frame_pose")
                measured_pose = (
                    measured_pose if isinstance(measured_pose, dict) else {}
                )
                orientation = measured_pose.get("orientation_xyzw")
                if not isinstance(orientation, list) or len(orientation) != 4:
                    raise RuntimeError(
                        "cannot cancel after Integrated alignment because Contact "
                        "has no measured orientation for a carry-preserving hold"
                    )
                contact.begin(
                    skill_id=SKILL_ID,
                    steps=[
                        contact_step(
                            position_m=[0.0, 0.0, 0.0],
                            orientation_xyzw=[
                                float(value) for value in orientation
                            ],
                        )
                    ],
                    carry_id=session["carry_id"],
                    attachment_revision=session["attachment_revision"],
                    behavior="CONTINUE",
                )
                recovered_contact_hold = contact.move(0)
            if contact.session_id is not None:
                contact.close(
                    "lay-flat development cancelled while preserving confirmed carry hold"
                )
            session["status"] = "CANCELLED"
            session["updated_at_us"] = time.time_ns() // 1000
            session["next_stage_number"] = None
            session["next_stage_deadline_at_us"] = None
            session["error"] = None
            session["cancel_disposition"] = (
                "CONFIRMED_CARRY_PRESERVED_IN_CONTACT_POS_TOR"
                if contact.session_id is not None
                else "CANCELLED_BEFORE_PHYSICAL_MOTION"
            )
            session["cancel_contact_hold"] = recovered_contact_hold
            return public_session(session) or {}
