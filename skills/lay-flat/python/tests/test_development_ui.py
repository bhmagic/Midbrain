from __future__ import annotations

from pathlib import Path
import asyncio
import json

import pytest

from grip_work_runtime.development import NumberedProfileStore
import grip_work_runtime
from lay_flat_skill.development_execution import LayFlatDevelopmentExecution
from lay_flat_skill.dev_ui import _motion_profile, _prepare, _vector_profile
from lay_flat_skill.host_adapter import LayFlatHostAdapter


ROOT = Path(__file__).resolve().parents[2]
WORKSPACE = ROOT.parents[1]


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


def test_lay_flat_development_prepare_exposes_all_four_vectors(tmp_path) -> None:
    vectors = _store(
        tmp_path,
        "vectors.json",
        "midbrain.lay_flat_gripper_vector_profiles",
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
        "midbrain.lay_flat_motion_profiles",
        {
            "name": "test",
            "table_inward_distance_m": 0.03,
            "negative_insertion_distance_m": 0.04,
            "retreat_distance_m": 0.05,
            "mit_delta_time_s": 0.5,
            "open_timeout_s": 5.0,
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
        },
        vectors,
        motion,
    )
    assert prepared["physical_motion_requested"] is False
    assert len(prepared["gripper_vectors"]) == 2
    assert len(prepared["object_vectors"]) == 2
    assert prepared["point_mode"] == "RELATIVE_TO_CURRENT_EFFECTOR_WORLD"
    assert prepared["approach_begin_point_world_m"] == [0.0, 0.0, 0.0]
    assert prepared["stages"][0] == "INTEGRATED_ROTATION_ONLY"
    assert prepared["stages"][1] == (
        "CONTACT_ABSOLUTE_APPROACH_PLUS_TABLE_INWARD_PLUS_NEGATIVE_INSERTION"
    )
    assert prepared["execution"] == (
        "SKILL_OWNED_ATTENDED_INTEGRATED_THEN_CONTACT_DEVELOPMENT"
    )
    assert len(prepared["stages"]) == 4
    assert [
        stage["stage_number"]
        for stage in LayFlatDevelopmentExecution.stage_definitions
    ] == [1, 2, 3, 4]


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
    def __init__(self, *_args, **_kwargs):
        pass

    def state(self):
        return {
            "carry": {
                "carry_id": "carry",
                "attachment_revision": "attachment",
            }
        }

    def command(self, **arguments):
        if arguments["operation"] == "RELEASE_OBJECT":
            return {"target_position_rad": 0.0}
        return {"operation": arguments["operation"]}

    def wait_for(self, predicate, **_kwargs):
        state = {"gripper_position_rad": 0.0, "state": "MIT_FLOAT"}
        assert predicate(state)
        return state


class _HostContact:
    calls = []

    def __init__(self, *_args, **_kwargs):
        type(self).calls = []

    def execute(self, **arguments):
        type(self).calls.append(arguments)
        return {"session_id": f"contact-{len(type(self).calls)}"}

    def relax(self, *_args):
        return {"relaxed": True}


def test_regular_lay_flat_uses_slicing_rotation_and_contact_modes(monkeypatch):
    monkeypatch.setattr(grip_work_runtime, "GripRuntime", _HostGrip)
    monkeypatch.setattr(grip_work_runtime, "ContactCarryRuntime", _HostContact)
    integrated = _HostIntegrated()
    manager = _HostManager(integrated)
    adapter = LayFlatHostAdapter(
        manager=manager,
        integrated_motion=integrated,
        contact_url="http://contact",
        grip_url="http://grip",
        effector_path=(
            WORKSPACE
            / "providers/rebot_arm_dm/profiles/effectors/"
            "rebot_b601_dm_bare_gripper_grip_control.v1.json"
        ),
        vector_profiles_path=ROOT / "config/gripper_vector_profiles.json",
        motion_profiles_path=ROOT / "config/motion_profiles.json",
    )

    result = asyncio.run(
        adapter.invoke(
            {
                "table_inward_direction_world": [0.0, 0.0, -1.0],
                "insertion_direction_world": [1.0, 0.0, 0.0],
                "gripper_vector_profile_number": 1,
            }
        )
    )

    assert integrated.preview_arguments["distance_m"] == 0.0
    assert integrated.preview_arguments["direction"] == "NONE"
    placement_step = _HostContact.calls[0]["steps"][0]
    retreat_step = _HostContact.calls[1]["steps"][0]
    assert placement_step["position_mode"] == "ABSOLUTE_ROOT"
    assert placement_step["position_m"] == pytest.approx([0.16, 0.1, 0.27])
    assert retreat_step["position_mode"] == (
        "RELATIVE_TO_MEASURED_EFFECTOR_ROOT_AXES"
    )
    assert manager.events[-2:] == [
        ("warm", "robot_arm.primary.integrated"),
        ("hot", "robot_arm.primary.contact"),
    ]
    assert result["workflow_complete"] is True
