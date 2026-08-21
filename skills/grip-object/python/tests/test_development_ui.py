from __future__ import annotations

import json
from threading import RLock
import time

from grip_object_skill.development_execution import ScrapGripDevelopmentExecution
from grip_object_skill.dev_ui import _motion_profile, _prepare, _vector_profile
from grip_work_runtime.development import NumberedProfileStore


def _store(tmp_path, name, schema, profile, validator):
    path = tmp_path / name
    path.write_text(
        json.dumps(
            {
                "schema": schema,
                "schema_version": 1,
                "default_profile_number": 1,
                "profiles": [{"profile_number": 1, **profile}],
            }
        ),
        encoding="utf-8",
    )
    return NumberedProfileStore(
        path,
        expected_schema=schema,
        validator=validator,
    )


def test_scrap_grip_development_prepare_exposes_all_four_vectors(tmp_path) -> None:
    vectors = _store(
        tmp_path,
        "vectors.json",
        "midbrain.scrap_grip_gripper_vector_profiles",
        {
            "name": "test",
            "table_inward_direction_effector": [0.0, 0.0, -1.0],
            "insertion_direction_effector": [1.0, 0.0, 0.0],
        },
        _vector_profile,
    )
    motion = _store(
        tmp_path,
        "motion.json",
        "midbrain.grip_motion_profiles",
        {
            "name": "test",
            "table_inward_distance_m": 0.03,
            "insertion_distance_m": 0.04,
            "delay_after_lower_s": 1.5,
            "delay_after_scrap_s": 1.5,
            "delay_after_grip_s": 1.5,
            "grip_position_rad": 0.20943951023931953,
            "grip_velocity_rad_s": 0.35,
            "gripping_torque_limit_nm": 0.7,
            "contact_timeout_s": 3.0,
        },
        _motion_profile,
    )
    prepared = _prepare(
        {
            "gripper_vector_profile_number": 1,
            "motion_profile_number": 1,
            "table_inward_direction_effector": [0.0, 0.0, -1.0],
            "insertion_direction_effector": [1.0, 0.0, 0.0],
            "table_inward_direction_world": [0.0, 0.0, -1.0],
            "insertion_direction_world": [1.0, 0.0, 0.0],
            "point_mode": "RELATIVE_TO_CURRENT_EFFECTOR_WORLD",
            "approach_begin_point_world_m": [0.0, 0.0, 0.0],
            "object_binding": {"object_id": "test"},
        },
        vectors,
        motion,
    )
    assert prepared["physical_motion_requested"] is False
    assert len(prepared["gripper_vectors"]) == 2
    assert len(prepared["object_vectors"]) == 2
    assert prepared["point_mode"] == "RELATIVE_TO_CURRENT_EFFECTOR_WORLD"
    assert prepared["approach_begin_point_world_m"] == [0.0, 0.0, 0.0]
    assert prepared["execution"] == (
        "SKILL_OWNED_ATTENDED_INTEGRATED_THEN_CONTACT_DEVELOPMENT"
    )
    assert prepared["stages"][0] == (
        "INTEGRATED_ROTATION_ONLY_WITH_CONCURRENT_GRIPPER_OPENING"
    )
    assert prepared["stages"][1] == (
        "CONTACT_ABSOLUTE_APPROACH_PLUS_TABLE_INWARD"
    )


class _AlignmentIntegrated:
    def __init__(self):
        self.handoff_requested = False

    def execute(self, _prepared):
        return {"final_state": "FLOAT", "goal_reached": True}

    def handoff_to_contact(self, _manager, provider_id):
        self.handoff_requested = True
        return {"contact_provider_id": provider_id}


class _UnusedContact:
    session_id = None

    @staticmethod
    def state():
        return {
            "provider_id": "robot_arm.primary.contact",
            "provider_instance_id": "contact-instance",
            "provider_boot_id": "contact-boot",
            "assembly_fingerprint": "assembly",
            "mounted_effector_revision": "effector",
        }


class _ApproachGrip:
    def __init__(self):
        self.commands = []

    @staticmethod
    def state():
        return {"thermal": {"ready_for_new_grip": True}}

    def command(self, **arguments):
        self.commands.append(arguments)
        return {
            "accepted": True,
            "state": "MIT_POSITION_TRANSITION",
            "requested_duration_s": arguments["duration_s"],
        }

    @staticmethod
    def wait_for(predicate, **_arguments):
        state = {
            "ready_for_approach": True,
            "functionally_open": True,
            "thermal": {"ready_for_new_grip": True},
        }
        assert predicate(state)
        return state


class _NoContactStageGrip:
    def __init__(self):
        self.commands = []
        self.released = False

    def command(self, **arguments):
        self.commands.append(arguments)
        return {"accepted": True, "operation": arguments["operation"]}

    @staticmethod
    def wait_for(*_args, **_arguments):
        raise TimeoutError("no stable contact at endpoint")

    def open_and_float(self, **_arguments):
        self.released = True
        return {"open_state": {"functionally_open": True}}


class _StageContact:
    session_id = "contact-session"

    def __init__(self):
        self.relaxed = False

    def relax_staged(self, _reason):
        self.relaxed = True
        return {"relaxed": True}


def test_stage_one_executes_one_rotation_and_hands_off_to_contact() -> None:
    execution = ScrapGripDevelopmentExecution.__new__(
        ScrapGripDevelopmentExecution
    )
    execution.lock = RLock()
    execution.manager = object()
    integrated = _AlignmentIntegrated()
    execution.session = {
        "session_id": "session",
        "status": "PREPARED",
        "next_stage_number": 1,
        "next_stage_deadline_at_us": time.time_ns() // 1000 + 30_000_000,
        "stage_results": [],
        "error": None,
        "_contact": _UnusedContact(),
        "_grip": _ApproachGrip(),
        "_integrated": integrated,
        "_integrated_prepared": {"preview": {"plan_id": "first-plan"}},
        "_plan": {
            "approach_open": {
                "position_rad": -3.141592653589793,
                "duration_s": 2.5,
                "kp": 8.0,
                "kd": 1.0,
            }
        },
    }
    execution._validate_session = lambda _session_id, _stage_number: (
        execution.session
    )

    session = execution.execute_stage(
        "session",
        1,
        physical_acknowledged=True,
    )

    assert session["status"] == "AWAITING_STAGE"
    assert session["next_stage_number"] == 2
    assert session["stage_results"][0]["stage_number"] == 1
    assert integrated.handoff_requested is True
    assert execution.session["_grip"].commands[0]["operation"] == (
        "SET_MIT_POSITION"
    )
    assert session["stage_results"][0]["result"][
        "gripper_opened_concurrently"
    ] is True
    assert session["contact_provider_identity"]["provider_id"] == (
        "robot_arm.primary.contact"
    )


def test_stage_four_missing_contact_is_terminal_and_released() -> None:
    execution = ScrapGripDevelopmentExecution.__new__(
        ScrapGripDevelopmentExecution
    )
    execution.lock = RLock()
    execution.manager = object()
    grip = _NoContactStageGrip()
    contact = _StageContact()
    execution.session = {
        "session_id": "session",
        "status": "AWAITING_STAGE",
        "next_stage_number": 4,
        "next_stage_deadline_at_us": time.time_ns() // 1000 + 30_000_000,
        "stage_results": [],
        "error": None,
        "carry_id": "carry",
        "attachment_revision": "attachment",
        "_contact": contact,
        "_grip": grip,
        "_plan": {
            "grip": {
                "position_rad": 0.20943951023931953,
                "velocity_limit_rad_s": 0.7,
                "torque_limit_nm": 0.7,
                "contact_timeout_s": 10.0,
            },
            "stage_waits_s": {"lower": 0.0, "scrap": 0.0, "grip": 0.0},
            "release": {
                "position_rad": -3.141592653589793,
                "position_tolerance_rad": 0.08,
                "velocity_limit_rad_s": 0.7,
                "torque_limit_nm": 0.35,
                "mit_delta_time_s": 0.5,
            },
            "attachment": {"object_binding": {"object_id": "test"}},
        },
    }
    execution._validate_session = lambda _session_id, _stage_number: (
        execution.session
    )

    session = execution.execute_stage(
        "session",
        4,
        physical_acknowledged=True,
    )

    result = session["stage_results"][-1]["result"]
    assert session["status"] == "GRIP_FAILED"
    assert session["next_stage_number"] is None
    assert result["status"] == "FAILED_TO_GRIP"
    assert result["grip_confirmed"] is False
    assert "12 degree endpoint" in result["message"]
    assert grip.released is True
    assert contact.relaxed is True
