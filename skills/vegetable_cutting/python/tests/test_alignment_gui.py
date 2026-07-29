from __future__ import annotations

from pathlib import Path

import pytest

from vegetable_cutting.alignment_gui import build_alignment_gui_command


def test_alignment_gui_command_reuses_core_without_opening_browser(
    tmp_path: Path,
) -> None:
    launcher = (
        tmp_path
        / "skills"
        / "stationary_world_arm_alignment"
        / "scripts"
        / "run_gui.ps1"
    )
    launcher.parent.mkdir(parents=True)
    launcher.write_text("# test launcher\n", encoding="utf-8")

    command = build_alignment_gui_command(
        tmp_path,
        powershell_executable="powershell-test",
    )

    assert command[0] == "powershell-test"
    assert command[command.index("-File") + 1] == str(launcher)
    assert "-NoCoreStart" in command
    assert "-NoBrowser" in command
    assert not any(
        term in " ".join(command).lower()
        for term in ("motion", "engage", "commit", "gripper", "float")
    )


def test_alignment_gui_command_rejects_missing_launcher(
    tmp_path: Path,
) -> None:
    with pytest.raises(RuntimeError, match="launcher is missing"):
        build_alignment_gui_command(tmp_path)
