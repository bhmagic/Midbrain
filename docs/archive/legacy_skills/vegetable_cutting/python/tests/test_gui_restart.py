from __future__ import annotations

from vegetable_cutting.app import gui_restart_block_reason


def test_gui_restart_is_blocked_during_active_integrated_motion() -> None:
    reason = gui_restart_block_reason(
        {"phase": "WAIT_FIRST_CUT_CONFIRMATION"},
        {"trajectory": {"active": True}},
    )

    assert reason is not None
    assert "trajectory is active" in reason


def test_gui_restart_is_blocked_during_physical_skill_phase() -> None:
    reason = gui_restart_block_reason(
        {"phase": "CUTTING"},
        {"trajectory": {"active": False}},
    )

    assert reason is not None
    assert "CUTTING" in reason


def test_gui_restart_is_allowed_while_integrated_is_idle() -> None:
    reason = gui_restart_block_reason(
        {"phase": "FAILED"},
        {"trajectory": {"active": False}},
    )

    assert reason is None
