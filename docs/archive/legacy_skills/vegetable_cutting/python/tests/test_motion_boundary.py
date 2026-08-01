from __future__ import annotations

import json
from pathlib import Path

from vegetable_cutting.clients import IntegratedControlClient


def test_integrated_client_exposes_supervised_motion_without_gripper_action() -> None:
    for required in (
        "engage",
        "teleop",
        "settings",
        "preview",
        "request_float",
        "safe_terminate",
    ):
        assert hasattr(IntegratedControlClient, required)
    assert not hasattr(IntegratedControlClient, "gripper")


def test_manifest_declares_calibration_and_takeover_boundary() -> None:
    manifest_path = Path(__file__).resolve().parents[2] / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    boundary = manifest["execution_boundary"]
    assert boundary["motion_submission"] == (
        "ENABLED_AFTER_SESSION_CALIBRATION_AND_OPERATOR_TAKEOVER"
    )
    assert boundary["motion_engage_allowed"] is True
    assert boundary["motion_commit_allowed"] is True
    assert boundary["gripper_command_allowed"] is False
    assert boundary["gravity_float_command_allowed"] is True
    assert (
        manifest["decision_policy"]["blade_registration_required_observations"]
        == 0
    )
    assert manifest["decision_policy"]["tool_registration_mode"] == (
        "FIXED_HARD_MOUNT"
    )
    assert manifest["decision_policy"]["payload_mass_kg"] == 0.07
    assert manifest["decision_policy"]["vlm_blade_registration_enabled"] is False
