from __future__ import annotations

from typing import Any

import pytest

from grip_work_runtime import ContactStagedRuntime, GripRuntime, contact_step


class FakeClient:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    def get(self, url: str) -> dict[str, Any]:
        assert url.endswith("/v1/contact/state")
        return {
            "ready": True,
            "provider_id": "robot_arm.primary.contact",
            "provider_instance_id": "contact-instance",
            "provider_boot_id": "contact-boot",
            "assembly_fingerprint": "assembly",
            "mounted_effector_revision": "effector",
            "acting_frame_id": "tool",
            "root_frame_id": "arm_base",
            "arm_resource_id": "robot_arm.primary",
        }

    def post(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        self.posts.append((url, payload, headers or {}))
        if url.endswith("/v1/control-authority/leases"):
            return {
                "resource_id": "robot_arm.primary",
                "lease_id": "lease",
                "owner_id": payload["owner_id"],
                "fencing_generation": 1,
                "permissions": payload["permissions"],
            }
        if url.endswith("/v1/contact/session"):
            return {"session_id": "contact-session"}
        if url.endswith("/v1/contact/move"):
            return {
                "sequence": payload["sequence"],
                "velocity_limited_transition_time_s": 0.0,
            }
        if url.endswith("/v1/contact/settling"):
            return {
                "settled": True,
                "trajectory_complete": True,
                "sequence": payload["sequence"],
            }
        if url.endswith("/v1/contact/carry/confirm"):
            return {"carry_confirmed": True}
        if url.endswith("/release"):
            return {"released": True}
        if url.endswith("/renew"):
            return {"renewed": True}
        raise AssertionError(f"unexpected POST {url}")


def test_staged_contact_plan_keeps_one_session_across_manual_moves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_CONTACT_SECRET", "x" * 32)
    client = FakeClient()
    runtime = ContactStagedRuntime(
        "http://contact",
        "http://manager",
        signing_secret_env="TEST_CONTACT_SECRET",
        client=client,
    )

    prepared = runtime.begin(
        skill_id="test.skill",
        steps=[
            contact_step(
                position_m=[0.1, 0.2, 0.3],
                orientation_xyzw=[0.0, 0.0, 0.0, 1.0],
                position_mode="ABSOLUTE_ROOT",
                delay_after_accept_s=0.0,
                next_command_timeout_s=60.0,
            ),
            contact_step(
                position_m=[0.0, 0.0, -0.01],
                orientation_xyzw=[0.0, 0.0, 0.0, 1.0],
                delay_after_accept_s=0.0,
                next_command_timeout_s=60.0,
            ),
        ],
        carry_id="carry",
        attachment_revision="attachment",
        behavior="PREPARE",
    )

    assert prepared["session_id"] == "contact-session"
    assert prepared["plan"]["steps"][0]["target"]["position_mode"] == "ABSOLUTE_ROOT"
    lease_payload = next(
        payload
        for url, payload, _headers in client.posts
        if url.endswith("/v1/control-authority/leases")
    )
    assert lease_payload["duration_ms"] == 60_000
    assert lease_payload["renewal_interval_ms"] == 5_000

    first = runtime.move(0)
    second = runtime.move(1)
    confirmed = runtime.confirm_staged_carry()
    runtime.close("test complete")

    assert first["next_command_deadline_at_us"] > 0
    assert second["settling"]["settled"] is True
    assert confirmed["carry_confirmed"] is True
    assert len(
        [url for url, _payload, _headers in client.posts if url.endswith("/v1/contact/session")]
    ) == 1
    assert any(url.endswith("/release") for url, _payload, _headers in client.posts)


def test_staged_contact_rejects_out_of_order_manual_move(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_CONTACT_SECRET", "x" * 32)
    runtime = ContactStagedRuntime(
        "http://contact",
        "http://manager",
        signing_secret_env="TEST_CONTACT_SECRET",
        client=FakeClient(),
    )
    runtime.begin(
        skill_id="test.skill",
        steps=[
            contact_step(
                position_m=[0.0, 0.0, 0.0],
                orientation_xyzw=[0.0, 0.0, 0.0, 1.0],
                delay_after_accept_s=0.0,
                next_command_timeout_s=60.0,
            )
        ],
        carry_id="carry",
        attachment_revision="attachment",
        behavior="PREPARE",
    )

    with pytest.raises(RuntimeError, match="next sequence is 0"):
        runtime.move(1)

    runtime.close("test cleanup")


def test_grip_wait_timeout_reports_contact_diagnostics() -> None:
    runtime = GripRuntime(
        "http://grip",
        signing_secret_env="UNUSED_TEST_SECRET",
    )
    runtime.state = lambda: {
        "state": "POS_TOR",
        "gripper_position_rad": -1.2,
        "gripper_velocity_rad_s": 0.3,
        "gripper_torque_nm": 0.04,
        "target": {"position_rad": -0.17453292519943295},
        "contact_inferred": False,
        "contact_stable_samples": 0,
    }

    with pytest.raises(TimeoutError) as error:
        runtime.wait_for(
            lambda state: state.get("contact_inferred") is True,
            timeout_s=0.0,
            description="stable gripper contact inference",
        )

    message = str(error.value)
    assert '"gripper_position_rad": -1.2' in message
    assert '"gripper_torque_nm": 0.04' in message
    assert '"contact_stable_samples": 0' in message


def test_grip_failed_release_verifies_open_before_float() -> None:
    runtime = GripRuntime(
        "http://grip",
        signing_secret_env="UNUSED_TEST_SECRET",
    )
    commands = []
    runtime.command = lambda **values: commands.append(values) or {
        "operation": values["operation"]
    }
    def wait_for(predicate, **values):
        state = (
            {"state": "MIT_FLOAT"}
            if "float" in values["description"]
            else {
                "functionally_open": True,
                "gripper_position_rad": -3.141592653589793,
            }
        )
        assert predicate(state)
        return state

    runtime.wait_for = wait_for

    result = runtime.open_and_float(
        skill_id="test.skill",
        execution_id="execution",
        position_rad=-3.141592653589793,
        velocity_limit_rad_s=0.7,
        torque_limit_nm=0.35,
        position_tolerance_rad=0.08,
        open_timeout_s=7.0,
        mit_delta_time_s=0.5,
    )

    assert [command["operation"] for command in commands] == [
        "SET_POSITION_EFFORT",
        "ENTER_MIT_FLOAT",
    ]
    assert commands[0]["intent"] == "OPEN"
    assert result["open_state"]["functionally_open"] is True
    assert result["float_state"]["state"] == "MIT_FLOAT"
