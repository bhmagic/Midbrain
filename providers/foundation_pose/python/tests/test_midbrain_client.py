from __future__ import annotations

import os

from foundation_pose_provider.midbrain_client import normalized_windows_environment


def test_windows_environment_merges_case_duplicate_path_values() -> None:
    result = normalized_windows_environment(
        {"Path": f"C:\\one{os.pathsep}C:\\shared", "PATH": f"C:\\two{os.pathsep}C:\\shared", "MODE": "test"}
    )
    assert "PATH" not in result
    assert result["Path"].split(os.pathsep) == ["C:\\one", "C:\\shared", "C:\\two"]
    assert result["MODE"] == "test"
