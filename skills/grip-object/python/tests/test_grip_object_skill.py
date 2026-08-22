from __future__ import annotations

from pathlib import Path
import asyncio
import json

import pytest

from grip_object_skill import build_plan
from grip_object_skill.host_adapter import GripObjectHostAdapter
import grip_work_runtime


ROOT = Path(__file__).resolve().parents[2]
WORKSPACE = ROOT.parents[1]


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_two_vector_plan_uses_live_defaults_and_exact_order():
    effector = load(
        WORKSPACE
        / "providers/rebot_arm_dm/profiles/effectors/rebot_b601_dm_bare_gripper_grip_control.v1.json"
    )
    profiles = load(ROOT / "config_templates/motion_profiles.default.json")
    vectors = load(ROOT / "config_templates/gripper_vector_profiles.default.json")
    plan = build_plan(
        {
            "approach_begin_point_world_m": [0.3, 0.1, 0.2],
            "table_inward_direction_world": [0.0, 0.0, -1.0],
            "insertion_direction_world": [1.0, 0.0, 0.0],
            "object_binding": {"object_id": "object-1"},
            "table_inward_distance_m": None,
            "insertion_distance_m": None,
            "gripping_torque_limit_nm": None,
        },
        effector_profile=effector,
        gripper_vector_profile=vectors["profiles"][0],
        motion_profile=profiles["profiles"][0],
        world_from_base={
            "translation_m": [0.0, 0.0, 0.0],
            "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
        },
    )
    assert plan["approach_begin_point_base_m"] == pytest.approx([0.3, 0.1, 0.2])
    assert plan["table_delta_base_m"] == pytest.approx([0.0, 0.0, -0.03])
    assert plan["insertion_delta_base_m"] == pytest.approx([0.04, 0.0, 0.0])
    assert plan["grip"]["torque_limit_nm"] == pytest.approx(0.7)
    assert plan["grip"]["position_rad"] == pytest.approx(0.20943951023931953)
    assert plan["grip"]["velocity_limit_rad_s"] == pytest.approx(4.0)
    assert plan["stage_waits_s"] == {
        "lower": 1.5,
        "scrap": 1.5,
        "grip": 1.5,
    }
    assert plan["approach_open"]["position_rad"] == pytest.approx(
        -3.141592653589793
    )
    assert plan["approach_open"]["duration_s"] == 1.0
    assert plan["approach_open"]["interpolation_rate_hz"] == 50.0
    assert plan["construction"].endswith("TABLE_INWARD_THEN_INSERT_THEN_GRIP")


def test_parallel_two_vector_inputs_are_rejected():
    effector = load(
        WORKSPACE
        / "providers/rebot_arm_dm/profiles/effectors/rebot_b601_dm_bare_gripper_grip_control.v1.json"
    )
    profiles = load(ROOT / "config_templates/motion_profiles.default.json")
    vectors = load(ROOT / "config_templates/gripper_vector_profiles.default.json")
    with pytest.raises(ValueError, match="projected insertion"):
        build_plan(
            {
                "approach_begin_point_world_m": [0.3, 0.1, 0.2],
                "table_inward_direction_world": [0.0, 0.0, -1.0],
                "insertion_direction_world": [0.0, 0.0, -2.0],
                "object_binding": {"object_id": "object-1"},
            },
            effector_profile=effector,
            gripper_vector_profile=vectors["profiles"][0],
            motion_profile=profiles["profiles"][0],
            world_from_base={
                "translation_m": [0.0, 0.0, 0.0],
                "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
            },
        )


class _HostIntegrated:
    def __init__(self):
        self.preview_arguments = None
        self.residency = "HOT"
        self.lease_active = True

    async def preview(self, **arguments):
        self.preview_arguments = arguments
        return {"status": "PREVIEW_READY", "preview_id": "plan-1"}

    async def execute_preview(self, *, preview_id):
        return {
            "workflow_complete": True,
            "physical_motion_completed": True,
            "goal_reached": True,
            "final_state": "FLOAT",
            "preview_id": preview_id,
        }

    async def observation(self):
        return {
            "controller": {
                "residency": self.residency,
                "boot_id": "boot",
                "provider_instance_id": "instance",
                "safety": {"float_confirmed": True},
                "trajectory": {"active": False},
                "lease": {"active": self.lease_active},
                "model_view": {
                    "measured_controlled_frame": {
                        "position_m": [0.2, 0.1, 0.3],
                        "rpy_rad": [0.0, 0.0, 0.0],
                    }
                },
                "planning": {
                    "last_authorized_transit": {"plan_id": "plan-1"}
                },
            }
        }


class _HostManager:
    base_url = "http://manager"

    def __init__(self, integrated):
        self.integrated = integrated
        self.events = []

    async def set_hot(self, provider_id):
        self.events.append(("hot", provider_id))

    async def set_residency(self, provider_id, action):
        self.events.append((action, provider_id))
        self.integrated.residency = "WARM"
        self.integrated.lease_active = False

    async def workcell_calibrations(self):
        return {
            "activations": [
                {
                    "state": "ACTIVE",
                    "motion_usable": True,
                    "activation_id": "activation",
                    "calibration_revision": "calibration",
                    "world_frame": "world",
                    "arm_base_frame": "rebot_arm_base",
                    "transforms": {
                        "world_from_base": {
                            "translation_m": [0.0, 0.0, 0.0],
                            "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
                        }
                    },
                }
            ]
        }


class _HostGrip:
    commands = []

    def __init__(self, *_args, **_kwargs):
        type(self).commands = []

    def state(self):
        return {"thermal": {"ready_for_new_grip": True}}

    def command(self, **arguments):
        type(self).commands.append(arguments)
        return {"operation": arguments["operation"]}

    def wait_for(self, predicate, *_args, **_kwargs):
        approach = {
            "ready_for_approach": True,
            "functionally_open": True,
            "thermal": {"ready_for_new_grip": True},
        }
        if predicate(approach):
            return approach
        contact = {"contact_inferred": True}
        assert predicate(contact)
        return contact


class _HostContact:
    last_steps = None

    def __init__(self, *_args, **_kwargs):
        pass

    def execute(self, **arguments):
        type(self).last_steps = arguments["steps"]
        return {"session_id": "contact-session"}

    def confirm_carry(self, *_args):
        return {"confirmed": True}

    def relax(self, *_args):
        return {"relaxed": True}


class _NoContactHostGrip(_HostGrip):
    released = False

    def __init__(self, *_args, **_kwargs):
        super().__init__(*_args, **_kwargs)
        type(self).released = False

    def wait_for(self, predicate, *_args, **_kwargs):
        approach = {
            "ready_for_approach": True,
            "functionally_open": True,
            "thermal": {"ready_for_new_grip": True},
        }
        if predicate(approach):
            return approach
        raise TimeoutError("no stable contact at endpoint")

    @classmethod
    def open_and_float(cls, **_arguments):
        cls.released = True
        return {"open_state": {"functionally_open": True}}


def test_regular_scrap_grip_uses_slicing_rotation_and_contact_modes(monkeypatch):
    observed_waits = []

    async def no_wait(delay_s):
        observed_waits.append(delay_s)

    monkeypatch.setattr(grip_work_runtime, "GripRuntime", _HostGrip)
    monkeypatch.setattr(grip_work_runtime, "ContactCarryRuntime", _HostContact)
    monkeypatch.setattr(asyncio, "sleep", no_wait)
    integrated = _HostIntegrated()
    manager = _HostManager(integrated)
    adapter = GripObjectHostAdapter(
        manager=manager,
        integrated_motion=integrated,
        contact_url="http://contact",
        grip_url="http://grip",
        effector_path=(
            WORKSPACE
            / "providers/rebot_arm_dm/profiles/effectors/"
            "rebot_b601_dm_bare_gripper_grip_control.v1.json"
        ),
        profiles_path=ROOT / "config_templates/motion_profiles.default.json",
        vector_profiles_path=(
            ROOT / "config_templates/gripper_vector_profiles.default.json"
        ),
    )

    result = asyncio.run(
        adapter.invoke(
            {
                "approach_begin_point_world_m": [0.3, 0.1, 0.2],
                "table_inward_direction_world": [0.0, 0.0, -1.0],
                "insertion_direction_world": [1.0, 0.0, 0.0],
                "object_binding": {"object_id": "object-1"},
                "gripper_vector_profile_number": 1,
            }
        )
    )

    assert integrated.preview_arguments["distance_m"] == 0.0
    assert integrated.preview_arguments["direction"] == "NONE"
    assert _HostContact.last_steps[0]["position_mode"] == "ABSOLUTE_ROOT"
    assert _HostContact.last_steps[0]["position_m"] == pytest.approx(
        [0.3, 0.1, 0.17]
    )
    assert _HostContact.last_steps[1]["position_mode"] == (
        "RELATIVE_TO_MEASURED_EFFECTOR_ROOT_AXES"
    )
    assert _HostContact.last_steps[0]["delay_after_accept_s"] == 1.5
    assert _HostContact.last_steps[1]["delay_after_accept_s"] == 1.5
    assert observed_waits == [1.5]
    assert manager.events[-2:] == [
        ("warm", "robot_arm.primary.integrated"),
        ("hot", "robot_arm.primary.contact"),
    ]
    assert [command["operation"] for command in _HostGrip.commands] == [
        "SET_MIT_POSITION",
        "SET_POSITION_EFFORT",
        "CONFIRM_CARRY",
    ]
    assert _HostGrip.commands[0]["position_rad"] == pytest.approx(
        -3.141592653589793
    )
    assert _HostGrip.commands[0]["duration_s"] == 1.0
    assert _HostGrip.commands[1]["velocity_limit_rad_s"] == pytest.approx(4.0)
    assert result["gripper_opened_concurrently"] is True
    assert result["workflow_complete"] is True


def test_agent_relative_begin_point_is_captured_from_measured_effector_fk(
    monkeypatch,
):
    async def no_wait(_delay_s):
        return None

    monkeypatch.setattr(grip_work_runtime, "GripRuntime", _HostGrip)
    monkeypatch.setattr(grip_work_runtime, "ContactCarryRuntime", _HostContact)
    monkeypatch.setattr(asyncio, "sleep", no_wait)
    integrated = _HostIntegrated()
    adapter = GripObjectHostAdapter(
        manager=_HostManager(integrated),
        integrated_motion=integrated,
        contact_url="http://contact",
        grip_url="http://grip",
        effector_path=(
            WORKSPACE
            / "providers/rebot_arm_dm/profiles/effectors/"
            "rebot_b601_dm_bare_gripper_grip_control.v1.json"
        ),
        profiles_path=ROOT / "config_templates/motion_profiles.default.json",
        vector_profiles_path=(
            ROOT / "config_templates/gripper_vector_profiles.default.json"
        ),
    )

    result = asyncio.run(
        adapter.invoke(
            {
                "approach_begin_point_world_m": [0.01, 0.02, -0.03],
                "point_mode": "RELATIVE_TO_CURRENT_EFFECTOR_WORLD",
                "table_inward_direction_world": [0.0, 0.0, -1.0],
                "insertion_direction_world": [1.0, 0.0, 0.0],
                "object_binding": {"object_id": "object-relative"},
                "gripper_vector_profile_number": 1,
            }
        )
    )

    assert _HostContact.last_steps[0]["position_m"] == pytest.approx(
        [0.21, 0.12, 0.24]
    )
    resolution = result["plan"]["approach_point_resolution"]
    assert resolution["captured_current_effector_base_m"] == pytest.approx(
        [0.2, 0.1, 0.3]
    )
    assert resolution["resolved_approach_begin_point_world_m"] == pytest.approx(
        [0.21, 0.12, 0.27]
    )


def test_regular_scrap_grip_releases_and_reports_missing_contact(monkeypatch):
    async def no_wait(_delay_s):
        return None

    monkeypatch.setattr(grip_work_runtime, "GripRuntime", _NoContactHostGrip)
    monkeypatch.setattr(grip_work_runtime, "ContactCarryRuntime", _HostContact)
    monkeypatch.setattr(asyncio, "sleep", no_wait)
    integrated = _HostIntegrated()
    adapter = GripObjectHostAdapter(
        manager=_HostManager(integrated),
        integrated_motion=integrated,
        contact_url="http://contact",
        grip_url="http://grip",
        effector_path=(
            WORKSPACE
            / "providers/rebot_arm_dm/profiles/effectors/"
            "rebot_b601_dm_bare_gripper_grip_control.v1.json"
        ),
        profiles_path=ROOT / "config_templates/motion_profiles.default.json",
        vector_profiles_path=(
            ROOT / "config_templates/gripper_vector_profiles.default.json"
        ),
    )

    result = asyncio.run(
        adapter.invoke(
            {
                "approach_begin_point_world_m": [0.3, 0.1, 0.2],
                "table_inward_direction_world": [0.0, 0.0, -1.0],
                "insertion_direction_world": [1.0, 0.0, 0.0],
                "object_binding": {"object_id": "object-1"},
                "gripper_vector_profile_number": 1,
            }
        )
    )

    assert result["status"] == "FAILED_TO_GRIP"
    assert result["workflow_complete"] is True
    assert result["grip_confirmed"] is False
    assert "12 degree endpoint" in result["message"]
    assert _NoContactHostGrip.released is True
