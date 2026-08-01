from __future__ import annotations

import numpy as np
import pytest

from vegetable_cutting.execution_preview import (
    build_execution_preview,
    desired_blade_frame,
)


def handoff() -> dict[str, object]:
    return {
        "minimum_approach_board_offset_mm": 60.0,
        "approach_clearance_above_vegetable_mm": 20.0,
        "requested_transfer_speed_m_s": 0.4,
        "transfer_speed_validated": False,
        "first_cut_human_confirmation_required": True,
        "cut_duration_s": 3.0,
        "mit_kp_multiplier": 1.0,
        "cut_target_board_offset_mm": 0.0,
        "allow_below_board_target": False,
    }


def cuts() -> list[dict[str, object]]:
    return [
        {
            "center_arm_base_m": [0.5, 0.0, 0.0],
            "entry_arm_base_m": [0.5, -0.03, 0.0],
            "exit_arm_base_m": [0.5, 0.03, 0.0],
        },
        {
            "center_arm_base_m": [0.52, 0.0, 0.0],
            "entry_arm_base_m": [0.52, -0.03, 0.0],
            "exit_arm_base_m": [0.52, 0.03, 0.0],
        },
    ]


def test_blade_yaw_rotates_around_arm_base_z() -> None:
    frame = desired_blade_frame(
        [0.0, 0.0, 0.0],
        [1.0, 0.0, 0.0],
        np.asarray([0.0, 0.0, 1.0]),
        90.0,
    )
    assert frame["edge_axis_arm_base"] == pytest.approx([0.0, 1.0, 0.0])
    assert frame["down_axis_arm_base"] == pytest.approx([0.0, 0.0, -1.0])
    assert frame["yaw_axis"] == "ARM_BASE_POSITIVE_Z"
    assert np.linalg.norm(frame["rotation_xyzw_arm_base"]) == pytest.approx(1.0)


def test_preview_uses_one_first_cut_vlm_gate_and_no_later_rechecks() -> None:
    preview = build_execution_preview(
        cuts(),
        np.asarray([0.0, 0.0, 1.0]),
        vegetable_maximum_height_mm=53.0,
        blade_yaw_deg=0.0,
        handoff=handoff(),
    )
    actions = [segment["action"] for segment in preview["segments"]]
    assert (
        actions.count("CLEARANCE_HEIGHT_FIRST_CUT_VISUAL_PREALIGNMENT")
        == 1
    )
    assert (
        actions.count("FIRST_CUT_HUMAN_REVIEW_OPTIONAL_VLM_RELATIVE")
        == 1
    )
    first_cut_gate = next(
        segment
        for segment in preview["segments"]
        if (
            segment["action"]
            == "FIRST_CUT_HUMAN_REVIEW_OPTIONAL_VLM_RELATIVE"
        )
    )
    assert first_cut_gate["backend"] == "HUMAN_GATE"
    assert first_cut_gate["vlm_called_only_after_human_rejection"] is False
    assert first_cut_gate["automatic_clearance_prealignment_vlm_calls"] == 1
    assert (
        first_cut_gate["additional_vlm_calls_require_human_rejection"]
        is True
    )
    assert first_cut_gate["maximum_vlm_calls"] is None
    assert first_cut_gate["operator_requested_vlm_rounds_unbounded"] is True
    assert actions.count("NO_COORDINATE_RECHECK_AFTER_HUMAN_APPROVAL") == 2
    assert "MIT_SHIFT_TO_NEXT_APPROACH" in actions
    controller_backends = {
        segment["backend"]
        for segment in preview["segments"]
        if segment["backend"] == "PRESS_MIT"
    }
    assert controller_backends == {"PRESS_MIT"}
    assert preview["approach_board_offset_mm"] == pytest.approx(73.0)
    assert preview["motion_submission_enabled"] is False
    assert preview["motion_submission_available_after_takeover"] is True
    assert preview["status"] == "REVIEW_REQUIRED_BEFORE_SUBMISSION"
    assert all(
        segment["motion_submitted"] is False
        for segment in preview["segments"]
    )
