from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from grip_skill.host_adapter import GripHostAdapter, measured_hold_step
import grip_work_runtime


SKILL_ROOT = Path(__file__).resolve().parents[2]


def test_measured_hold_step_preserves_orientation_and_has_zero_translation() -> None:
    step = measured_hold_step(
        {
            "measured_acting_frame_pose": {
                "orientation_xyzw": [0.0, 0.0, 0.0, 2.0]
            }
        }
    )
    assert step == {
        "position_m": [0.0, 0.0, 0.0],
        "orientation_xyzw": [0.0, 0.0, 0.0, 1.0],
    }


@pytest.mark.parametrize(
    "state",
    [
        {},
        {"measured_acting_frame_pose": None},
        {"measured_acting_frame_pose": {"orientation_xyzw": [0.0, 0.0, 0.0]}},
        {"measured_acting_frame_pose": {"orientation_xyzw": [0.0, 0.0, 0.0, 0.0]}},
    ],
)
def test_measured_hold_step_rejects_missing_or_invalid_pose(state) -> None:
    with pytest.raises(RuntimeError):
        measured_hold_step(state)


def test_default_motion_profile_is_loadable() -> None:
    adapter = GripHostAdapter(
        manager=object(),
        integrated_motion=object(),
        contact_url="http://contact",
        grip_url="http://grip",
        profiles_path=SKILL_ROOT / "config_templates/motion_profiles.default.json",
        effector_path=(
            SKILL_ROOT.parents[1]
            / "providers/rebot_arm_dm/profiles/effectors/rebot_b601_dm_bare_gripper_grip_control.v1.json"
        ),
    )
    profile = adapter._profile(None)
    assert profile["profile_number"] == 1
    assert profile["grip_position_rad"] == pytest.approx(0.20943951023931953)
    assert profile["grip_velocity_rad_s"] == pytest.approx(4.0)
    assert profile["gripping_torque_limit_nm"] == 0.7


def test_manifest_exposes_generic_grip_without_reusing_scrap_identity() -> None:
    manifest = json.loads((SKILL_ROOT / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["display_name"] == "Action: grip"
    assert manifest["agent_discovery"]["tool_name"] == "grip"
    assert manifest["agent_discovery"]["execution_adapter"]["adapter_id"] == "skill.grip.host.v1"
    assert manifest["route_policy"]["arm_motion"] == "MEASURED_POSE_HOLD_ONLY"


class _Manager:
    base_url = "http://manager"

    def __init__(self):
        self.integrated_residency = "HOT"
        self.hot_requests = []

    async def set_hot(self, provider_id):
        self.hot_requests.append(provider_id)
        if provider_id == "robot_arm.primary.integrated":
            self.integrated_residency = "HOT"
        return None

    async def set_residency(self, provider_id, action):
        assert provider_id == "robot_arm.primary.integrated"
        assert action == "warm"
        self.integrated_residency = "WARM"
        return None


class _IntegratedMotion:
    def __init__(self, manager):
        self.manager = manager

    async def observation(self):
        warm = self.manager.integrated_residency == "WARM"
        return {
            "controller": {
                "residency": self.manager.integrated_residency,
                "safety": {"float_confirmed": True},
                "trajectory": {"active": False},
                "lease": {"active": not warm},
            }
        }


class _NoContactGrip:
    released = False

    def __init__(self, *_args, **_kwargs):
        type(self).released = False

    @staticmethod
    def state():
        return {"thermal": {"ready_for_new_grip": True}}

    @staticmethod
    def command(**_arguments):
        return {"accepted": True}

    @staticmethod
    def wait_for(*_args, **_kwargs):
        raise TimeoutError("no stable contact at endpoint")

    @classmethod
    def open_and_float(cls, **_arguments):
        cls.released = True
        return {"open_state": {"functionally_open": True}}


class _MeasuredHoldContact:
    relaxed = False

    def __init__(self, *_args, **_kwargs):
        type(self).relaxed = False

    @staticmethod
    def state():
        return {
            "measured_acting_frame_pose": {
                "orientation_xyzw": [0.0, 0.0, 0.0, 1.0]
            }
        }

    @staticmethod
    def execute(**_arguments):
        return {"session_id": "contact-session"}

    @classmethod
    def relax(cls, *_arguments):
        cls.relaxed = True
        return {"relaxed": True}


def test_missing_contact_releases_and_returns_failed_to_grip(monkeypatch) -> None:
    monkeypatch.setattr(grip_work_runtime, "GripRuntime", _NoContactGrip)
    monkeypatch.setattr(
        grip_work_runtime, "ContactCarryRuntime", _MeasuredHoldContact
    )
    manager = _Manager()
    adapter = GripHostAdapter(
        manager=manager,
        integrated_motion=_IntegratedMotion(manager),
        contact_url="http://contact",
        grip_url="http://grip",
        profiles_path=SKILL_ROOT / "config_templates/motion_profiles.default.json",
        effector_path=(
            SKILL_ROOT.parents[1]
            / "providers/rebot_arm_dm/profiles/effectors/"
            "rebot_b601_dm_bare_gripper_grip_control.v1.json"
        ),
    )

    result = asyncio.run(
        adapter.invoke({"object_binding": {"object_id": "test-object"}})
    )

    assert result["status"] == "FAILED_TO_GRIP"
    assert result["workflow_complete"] is True
    assert result["task_success"] is False
    assert result["grip_confirmed"] is False
    assert "12 degree endpoint" in result["message"]
    assert _NoContactGrip.released is True
    assert _MeasuredHoldContact.relaxed is True
    assert manager.hot_requests == [
        "robot_arm.primary.grip",
        "robot_arm.primary.integrated",
        "robot_arm.primary.contact",
    ]
