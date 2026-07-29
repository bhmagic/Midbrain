from __future__ import annotations

from pathlib import Path


def build_alignment_gui_command(
    workspace_root: Path,
    *,
    powershell_executable: str = "powershell.exe",
) -> list[str]:
    root = Path(workspace_root).resolve()
    launcher = (
        root
        / "skills"
        / "stationary_world_arm_alignment"
        / "scripts"
        / "run_gui.ps1"
    )
    if not launcher.is_file():
        raise RuntimeError(f"alignment GUI launcher is missing: {launcher}")
    return [
        powershell_executable,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(launcher),
        "-NoBrowser",
        "-NoCoreStart",
    ]
