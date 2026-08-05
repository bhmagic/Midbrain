from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


PROVIDER_ROOT = Path(__file__).resolve().parents[2]


def _load_provider_module():
    spec = importlib.util.spec_from_file_location(
        "sam2_scene_tracker_provider", PROVIDER_ROOT / "provider.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_checked_in_config_has_no_bootstrap_semantic_policy() -> None:
    config = json.loads(
        (PROVIDER_ROOT / "config_templates" / "tracker.default.json").read_text(
            encoding="utf-8"
        )
    )

    assert "bootstrap_policy" not in config
    assert config["policy_stream"] == "robot_arm.scene.segmentation_policy"
    assert config["tracking_stationary_interval_s"] == 4.0
    assert config["tracking_motion_interval_s"] == 0.8
    assert config["vlm_stationary_refresh_interval_s"] == 40.0
    assert config["vlm_motion_refresh_interval_s"] == 20.0


def test_bootstrap_semantic_policy_is_rejected(tmp_path: Path) -> None:
    provider = _load_provider_module()
    config_path = tmp_path / "tracker.json"
    config_path.write_text(
        json.dumps(
            {
                "policy_stream": "robot_arm.scene.segmentation_policy",
                "bootstrap_policy": {"objects": []},
                "vlm_candidates": [{"backend": "test", "model": "test"}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="bootstrap_policy is not supported"):
        provider.load_config(config_path)
