from __future__ import annotations

from pathlib import Path
import asyncio
import json

import grip_work_runtime

from let_go_skill.host_adapter import LetGoHostAdapter


WORKSPACE = Path(__file__).resolve().parents[4]
EFFECTOR_PATH = (
    WORKSPACE
    / "providers/rebot_arm_dm/profiles/effectors/rebot_b601_dm_bare_gripper_grip_control.v1.json"
)


class FakeManager:
    base_url = "http://manager"

    def __init__(self) -> None:
        self.hot_requests: list[str] = []

    async def set_hot(self, provider_id: str) -> None:
        self.hot_requests.append(provider_id)


class FakeGrip:
    def __init__(self, carry=None) -> None:
        self.carry = carry
        self.commands: list[dict] = []

    def state(self) -> dict:
        return {"carry": self.carry}

    def command(self, **arguments) -> dict:
        self.commands.append(arguments)
        if arguments["operation"] in {"RELEASE_OBJECT", "SET_POSITION_EFFORT"}:
            return {
                "accepted": True,
                "target_position_rad": -3.141592653589793,
            }
        return {"accepted": True, "state": "MIT_FLOAT_TRANSITION"}

    @staticmethod
    def wait_for(predicate, *, description, **_arguments) -> dict:
        if description == "measured open gripper":
            state = {
                "gripper_position_rad": -3.141592653589793,
                "gripper_velocity_rad_s": 0.0,
            }
        else:
            state = {"state": "MIT_FLOAT"}
        assert predicate(state)
        return state


class FakeContact:
    def __init__(self) -> None:
        self.relax_calls: list[tuple[str, str]] = []

    @staticmethod
    def state() -> dict:
        return {"session_id": "contact-session"}

    def relax(self, session_id: str, reason: str) -> dict:
        self.relax_calls.append((session_id, reason))
        return {"relaxed": True}


def test_let_go_opens_unbound_gripper_without_contact(monkeypatch) -> None:
    manager = FakeManager()
    grip = FakeGrip()

    monkeypatch.setattr(grip_work_runtime, "GripRuntime", lambda *_args, **_kwargs: grip)

    def reject_contact(*_args, **_kwargs):
        raise AssertionError("unbound gripper opening must not acquire Contact")

    monkeypatch.setattr(grip_work_runtime, "ContactCarryRuntime", reject_contact)
    adapter = LetGoHostAdapter(
        manager=manager,
        contact_url="http://contact",
        grip_url="http://grip",
        effector_path=EFFECTOR_PATH,
    )

    result = asyncio.run(adapter.invoke({}))

    assert result["status"] == "GRIPPER_OPENED_AND_FLOATED"
    assert result["released_carry_id"] is None
    assert result["contact_relax"] is None
    assert manager.hot_requests == ["robot_arm.primary.grip"]
    assert grip.commands[0]["operation"] == "SET_POSITION_EFFORT"
    assert grip.commands[0]["intent"] == "OPEN"
    assert grip.commands[0]["position_rad"] == -3.141592653589793
    assert grip.commands[0]["velocity_limit_rad_s"] == 4.0
    assert grip.commands[1]["operation"] == "ENTER_MIT_FLOAT"


def test_let_go_preserves_confirmed_carry_release_and_contact_relax(monkeypatch) -> None:
    manager = FakeManager()
    grip = FakeGrip({"carry_id": "carry-1"})
    contact = FakeContact()
    monkeypatch.setattr(grip_work_runtime, "GripRuntime", lambda *_args, **_kwargs: grip)
    monkeypatch.setattr(
        grip_work_runtime,
        "ContactCarryRuntime",
        lambda *_args, **_kwargs: contact,
    )
    adapter = LetGoHostAdapter(
        manager=manager,
        contact_url="http://contact",
        grip_url="http://grip",
        effector_path=EFFECTOR_PATH,
    )

    result = asyncio.run(adapter.invoke({}))

    assert result["status"] == "OBJECT_RELEASED_AND_ARM_RELAXED"
    assert result["released_carry_id"] == "carry-1"
    assert grip.commands[0]["operation"] == "RELEASE_OBJECT"
    assert contact.relax_calls[0][0] == "contact-session"


def test_manifest_allows_null_released_carry_id() -> None:
    manifest = json.loads(
        (WORKSPACE / "skills/let-go/manifest.json").read_text(encoding="utf-8")
    )
    output = manifest["agent_discovery"]["output_schema"]
    assert output["properties"]["released_carry_id"]["type"] == ["string", "null"]
    assert manifest["agent_discovery"]["input_schema"]["properties"][
        "open_timeout_s"
    ]["default"] == 10.0
