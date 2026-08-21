from __future__ import annotations

from pathlib import Path
import copy
import json
import os
import threading
import time
import uuid

import pytest

from rebot_arm_grip.authorization import canonical_sha256, sign_assertion
from rebot_arm_grip.basic_client import BasicLease
from rebot_arm_grip.controller import (
    GripController,
    MitPositionHold,
    ThermalGateError,
)


ROOT = Path(__file__).resolve().parents[2]
WORKSPACE = ROOT.parents[1]
SECRET = "grip-provider-test-secret-that-is-long-enough"


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8-sig") as stream:
        return json.load(stream)


class FakeBasic:
    def __init__(self):
        self.model_value = load_json(
            WORKSPACE / "providers/rebot_arm_dm/config_templates/arm_model.factory.json"
        )
        mounted = load_json(
            WORKSPACE
            / "providers/rebot_arm_dm/profiles/effectors/rebot_b601_dm_bare_gripper.v2.json"
        )
        self.assembly_value = {
            "assembly_fingerprint": "a" * 64,
            "mounted_effector": mounted,
            "resource_groups": [
                {
                    "group_id": "arm",
                    "resource_id": "robot_arm.primary/arm",
                    "joint_names": [f"joint{i}" for i in range(1, 7)],
                },
                {
                    "group_id": "gripper",
                    "resource_id": "robot_arm.primary/gripper",
                    "joint_names": ["gripper"],
                },
            ],
        }
        self.state_value = {
            "feedback_age_ms": 1.0,
            "positions_rad": [0.0] * 6 + [-1.0],
            "velocities_rad_s": [0.0] * 7,
            "torques_nm": [0.0] * 6 + [0.2],
            "temperatures_c": [35.0] * 7,
            "active_command_modes": ["POSITION_EFFORT_LIMITED"] * 7,
        }
        self.lease = None
        self.commands = []
        self.guards = []
        self.float_calls = 0
        self.bind_calls = []

    def model(self):
        return copy.deepcopy(self.model_value)

    def assembly(self):
        return copy.deepcopy(self.assembly_value)

    def state(self):
        return copy.deepcopy(self.state_value)

    def bind_resource(self, resource_id, joint_index):
        if self.lease is not None:
            raise RuntimeError("cannot change gripper resource while leased")
        self.resource_id = resource_id
        self.joint_index = joint_index
        self.bind_calls.append((resource_id, joint_index))

    def lease_snapshot(self):
        return copy.deepcopy(self.lease)

    def acquire(self, holder, duration_ms):
        self.lease = BasicLease(
            "lease-1",
            4,
            self.resource_id,
            time.monotonic() + duration_ms / 1000.0,
        )
        return copy.deepcopy(self.lease)

    def renew(self, duration_ms):
        self.lease.expires_monotonic = time.monotonic() + duration_ms / 1000.0
        return copy.deepcopy(self.lease)

    def command(self, mode, values, timeout_ms):
        self.commands.append((mode, copy.deepcopy(values), timeout_ms))
        self.state_value["active_command_modes"][6] = mode
        return {"accepted": True}

    def set_required_command_mode(self, mode):
        if mode is not None and (
            not self.commands or self.commands[-1][0] != mode
        ):
            raise RuntimeError(
                "pending group command does not completely match the requested mode guard"
            )
        self.guards.append(mode)
        self.lease.required_command_mode = mode
        return copy.deepcopy(self.lease)

    def float(self, reason):
        self.float_calls += 1
        self.state_value["active_command_modes"][6] = "IMPEDANCE"
        return {"accepted": True}

    def release(self, reason):
        self.lease = None


class FakeContactHttp:
    def __init__(self):
        self.value = {
            "carry": {
                "confirmed": True,
                "carry_id": "carry-1",
                "attachment_revision": "attachment-1",
            }
        }

    def get(self, url):
        return copy.deepcopy(self.value)


@pytest.fixture
def controller(monkeypatch):
    monkeypatch.setenv("MIDBRAIN_GRIP_GENERIC_SECRET", SECRET)
    monkeypatch.setenv("MIDBRAIN_GRIP_OBJECT_SECRET", SECRET)
    monkeypatch.setenv("MIDBRAIN_GRIP_LET_GO_SECRET", SECRET)
    config = load_json(ROOT / "config_templates/controller.default.json")
    config["effector_control_profile"] = load_json(
        WORKSPACE
        / "providers/rebot_arm_dm/profiles/effectors/rebot_b601_dm_bare_gripper_grip_control.v1.json"
    )
    basic = FakeBasic()
    value = GripController(
        config,
        basic,
        FakeContactHttp(),
        provider_instance_id="instance-1",
        provider_boot_id="boot-1",
    )
    value._initialize()
    return value


def command_for(controller, operation, *, skill_id="grip.grip_object", **values):
    command = {
        "schema": "midbrain.grip_control_command",
        "schema_version": 1,
        "command_id": f"command-{uuid.uuid4()}",
        "skill_id": skill_id,
        "execution_id": f"execution-{uuid.uuid4()}",
        "operation": operation,
        **values,
    }
    now = time.time_ns() // 1000
    assertion = sign_assertion(
        {
            "schema": "midbrain.grip_control_authorization",
            "schema_version": 1,
            "assertion_id": f"assertion-{uuid.uuid4()}",
            "nonce": "0123456789abcdef0123456789abcdef",
            "issuer_skill_id": skill_id,
            "execution_id": command["execution_id"],
            "audience_provider_id": controller.provider_id,
            "provider_instance_id": controller.provider_instance_id,
            "provider_boot_id": controller.provider_boot_id,
            "assembly_fingerprint": controller.assembly_fingerprint,
            "mounted_effector_revision": controller.mounted_effector_revision,
            "command_sha256": canonical_sha256(command),
            "issued_at_us": now,
            "expires_at_us": now + 30_000_000,
        },
        SECRET,
    )
    return command, assertion


def test_new_grip_rejects_any_active_joint_at_thermal_gate(controller):
    controller.basic.state_value["temperatures_c"][2] = 85.0
    command, assertion = command_for(
        controller,
        "SET_POSITION_EFFORT",
        position_rad=-0.17453292519943295,
        torque_limit_nm=0.7,
        intent="GRIP",
    )
    with pytest.raises(ThermalGateError) as error:
        controller.submit(command, assertion)
    assert error.value.retry_after_s == 60.0
    assert controller.basic.commands == []

    controller.basic.state_value["temperatures_c"][2] = 84.999
    command, assertion = command_for(
        controller,
        "SET_POSITION_EFFORT",
        position_rad=-0.17453292519943295,
        torque_limit_nm=0.7,
        intent="GRIP",
    )
    assert controller.submit(command, assertion)["accepted"] is True


def test_generic_grip_skill_is_authorized_for_bounded_position_effort(controller):
    command, assertion = command_for(
        controller,
        "SET_POSITION_EFFORT",
        skill_id="grip.grip",
        position_rad=-0.17453292519943295,
        torque_limit_nm=0.7,
        intent="GRIP",
    )
    result = controller.submit(command, assertion)
    assert result["accepted"] is True
    assert result["state"] == "POS_TOR"


def test_repeated_hot_is_idempotent_while_the_gripper_lease_is_active(controller):
    class LiveThread:
        @staticmethod
        def is_alive():
            return True

    controller.basic.acquire("test", 3000)
    controller.thread = LiveThread()
    controller.residency = "HOT"

    controller.start()

    assert controller.basic.bind_calls == [
        ("robot_arm.primary/gripper", 6)
    ]
    assert controller.basic.lease_snapshot() is not None


def test_position_effort_transition_serializes_against_previous_mit_hold(controller):
    controller.basic.acquire("test", 3000)
    controller.mit_position_hold = MitPositionHold(
        position_rad=-3.141592653589793,
        target_rate_limit_rad_s=0.7,
        kp=8.0,
        kd=1.0,
    )
    controller.state = "MIT_POSITION_HOLD"
    original_send_target = controller._send_target
    race_started = threading.Event()
    race_finished = threading.Event()
    race_threads = []
    injected = False

    def run_competing_tick():
        race_started.set()
        with controller.operation_lock:
            controller._tick()
        race_finished.set()

    def send_target_with_competing_tick(target):
        nonlocal injected
        original_send_target(target)
        if injected:
            return
        injected = True
        worker = threading.Thread(target=run_competing_tick)
        race_threads.append(worker)
        worker.start()
        assert race_started.wait(1.0)
        time.sleep(0.02)
        assert race_finished.is_set() is False

    controller._send_target = send_target_with_competing_tick
    command, assertion = command_for(
        controller,
        "SET_POSITION_EFFORT",
        position_rad=-0.17453292519943295,
        torque_limit_nm=0.7,
        intent="GRIP",
    )

    result = controller.submit(command, assertion)

    for worker in race_threads:
        worker.join(timeout=1.0)
    assert race_finished.is_set() is True
    assert result["state"] == "POS_TOR"
    assert controller.basic.guards[-1] == "POSITION_EFFORT_LIMITED"
    assert all(
        mode == "POSITION_EFFORT_LIMITED"
        for mode, _values, _timeout in controller.basic.commands
    )


def test_mit_position_opening_uses_one_second_at_four_rad_s_and_holds_functionally_open(controller):
    controller.basic.state_value["positions_rad"][6] = -0.17453292519943295
    command, assertion = command_for(
        controller,
        "SET_MIT_POSITION",
        position_rad=-3.141592653589793,
        duration_s=1.0,
        intent="OPEN",
    )

    result = controller.submit(command, assertion)

    assert result["state"] == "MIT_POSITION_TRANSITION"
    assert result["requested_duration_s"] == 1.0
    assert result["resolved_duration_s"] == pytest.approx(1.0)
    assert result["target_rate_limit_rad_s"] == pytest.approx(
        abs(-3.141592653589793 + 0.17453292519943295)
    )
    assert controller.basic.guards == [None]

    controller.mit_position_transition.started_monotonic -= (
        result["resolved_duration_s"] + 1.0
    )
    controller.basic.state_value["positions_rad"][6] = -3.141592653589793
    controller._tick()

    assert controller.state == "MIT_POSITION_HOLD"
    assert controller.basic.commands[-1][0] == "IMPEDANCE"
    assert controller.basic.commands[-1][1]["position_rad"] == pytest.approx(
        -3.141592653589793
    )
    state = controller.snapshot()
    assert state["functionally_open"] is True
    assert state["ready_for_approach"] is True
    assert state["gripper_torque_nm"] == pytest.approx(0.2)

    command_count = len(controller.basic.commands)
    controller._tick()
    assert len(controller.basic.commands) == command_count + 1
    assert controller.basic.commands[-1][0] == "IMPEDANCE"


def test_mit_position_opening_uses_requested_2_5_seconds_when_within_velocity_cap(
    controller,
):
    controller.basic.state_value["positions_rad"][6] = -2.0
    command, assertion = command_for(
        controller,
        "SET_MIT_POSITION",
        position_rad=-3.141592653589793,
        duration_s=2.5,
        intent="OPEN",
    )

    result = controller.submit(command, assertion)

    assert result["resolved_duration_s"] == 2.5
    assert result["target_rate_limit_rad_s"] == pytest.approx(
        abs(-3.141592653589793 + 2.0) / 2.5
    )


def test_mit_open_hold_to_position_effort_grip_has_no_float_gap(controller):
    opening, opening_assertion = command_for(
        controller,
        "SET_MIT_POSITION",
        position_rad=-3.141592653589793,
        duration_s=1.0,
        intent="OPEN",
    )
    controller.submit(opening, opening_assertion)
    controller.mit_position_transition.started_monotonic -= 2.0
    controller.basic.state_value["positions_rad"][6] = -3.141592653589793
    controller._tick()

    closing, closing_assertion = command_for(
        controller,
        "SET_POSITION_EFFORT",
        position_rad=0.20943951023931953,
        velocity_limit_rad_s=4.0,
        torque_limit_nm=0.7,
        intent="GRIP",
    )
    controller.submit(closing, closing_assertion)

    assert controller.basic.commands[-2][0] == "IMPEDANCE"
    assert controller.basic.commands[-1][0] == "POSITION_EFFORT_LIMITED"
    assert controller.basic.guards == [None, "POSITION_EFFORT_LIMITED"]
    assert controller.basic.float_calls == 0


def test_mit_position_opening_requires_thermal_readiness_and_open_target(controller):
    command, assertion = command_for(
        controller,
        "SET_MIT_POSITION",
        position_rad=-0.17453292519943295,
        duration_s=2.5,
        intent="OPEN",
    )
    with pytest.raises(ValueError, match="not functionally open"):
        controller.submit(command, assertion)

    controller.basic.state_value["temperatures_c"][1] = 85.0
    command, assertion = command_for(
        controller,
        "SET_MIT_POSITION",
        position_rad=-3.141592653589793,
        duration_s=2.5,
        intent="OPEN",
    )
    with pytest.raises(ThermalGateError):
        controller.submit(command, assertion)


def test_carry_requires_contact_inference_contact_binding_and_all_pos_tor(controller):
    command, assertion = command_for(
        controller,
        "SET_POSITION_EFFORT",
        position_rad=-0.17453292519943295,
        torque_limit_nm=0.7,
        intent="GRIP",
    )
    result = controller.submit(command, assertion)
    assert result["state"] == "POS_TOR"
    assert controller.basic.guards == ["POSITION_EFFORT_LIMITED"]
    for _ in range(10):
        controller._tick()
    assert controller.contact_inferred is True
    confirm, confirm_assertion = command_for(
        controller,
        "CONFIRM_CARRY",
        carry_id="carry-1",
        attachment_revision="attachment-1",
        attachment={"object_binding": {"object_id": "object-1"}},
    )
    confirmed = controller.submit(confirm, confirm_assertion)
    assert confirmed["state"] == "CARRYING_POS_TOR"
    assert controller.snapshot()["all_active_joints_position_effort_limited"] is True


def test_contact_inference_uses_only_stable_absolute_measured_torque(controller):
    controller.basic.state_value["positions_rad"][6] = 0.20943951023931953
    controller.basic.state_value["velocities_rad_s"][6] = 8.0
    controller.basic.state_value["torques_nm"][6] = 0.149

    for _ in range(12):
        controller._update_contact_inference(controller.basic.state())
    assert controller.contact_inferred is False
    assert controller.contact_samples == 0

    controller.basic.state_value["torques_nm"][6] = -0.15
    for _ in range(9):
        controller._update_contact_inference(controller.basic.state())
    assert controller.contact_inferred is False
    assert controller.contact_samples == 9

    controller._update_contact_inference(controller.basic.state())
    assert controller.contact_inferred is True
    assert controller.contact_samples == 10


def test_release_is_allowed_hot_then_timed_mit_transition_finishes_group_float(controller):
    controller.carry = {
        "carry_id": "carry-1",
        "attachment_revision": "attachment-1",
    }
    controller.target = controller._validate_target(
        {
            "position_rad": -0.17453292519943295,
            "torque_limit_nm": 0.7,
            "intent": "GRIP",
        }
    )
    controller.basic.state_value["temperatures_c"] = [90.0] * 7
    release, release_assertion = command_for(
        controller,
        "RELEASE_OBJECT",
        skill_id="grip.let_go",
        carry_id="carry-1",
    )
    assert controller.submit(release, release_assertion)["state"] == "RELEASING_POS_TOR"
    transition, transition_assertion = command_for(
        controller,
        "ENTER_MIT_FLOAT",
        skill_id="grip.let_go",
        delta_time_s=0.02,
    )
    controller.submit(transition, transition_assertion)
    controller.float_transition.started_monotonic -= 1.0
    controller._tick()
    assert controller.state == "MIT_FLOAT"
    assert controller.basic.float_calls == 1
    assert controller.basic.lease is None


def test_carry_mode_audit_never_silently_floats(controller):
    controller.carry = {
        "carry_id": "carry-1",
        "attachment_revision": "attachment-1",
    }
    controller.basic.state_value["active_command_modes"][3] = "IMPEDANCE"
    controller._audit_carry_modes(controller.basic.state())
    assert controller.state == "DEGRADED"
    assert controller.basic.float_calls == 0


def test_carry_rejects_empty_object_binding_and_nonfinite_contact_torque(controller):
    target = controller._validate_target(
        {
            "position_rad": -0.17453292519943295,
            "torque_limit_nm": 0.7,
            "intent": "GRIP",
        }
    )
    controller.target = target
    controller.basic.state_value["torques_nm"][6] = float("inf")
    for _ in range(12):
        controller._update_contact_inference(controller.basic.state())
    assert controller.contact_inferred is False

    controller.contact_inferred = True
    confirm, assertion = command_for(
        controller,
        "CONFIRM_CARRY",
        carry_id="carry-1",
        attachment_revision="attachment-1",
        attachment={"object_binding": {}},
    )
    with pytest.raises(ValueError, match="non-empty"):
        controller.submit(confirm, assertion)


def test_mit_transition_rejects_nonfinite_or_negative_gains(controller):
    for values in ({"kp": float("inf")}, {"kd": -0.1}):
        with pytest.raises(ValueError, match="MIT gains"):
            controller._enter_mit_float({"delta_time_s": 0.5, **values})
