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
    assert config["tracking_rate_hz"] == 1.0
    assert config["angular_direction_count"] == 4096
    assert config["angular_minimum_radius_m"] == 0.005
    assert config["aabb_freshness_ms"] == 5000
    assert config["work_object_mask_erosion_m"] == 0.01
    assert config["keep_out_mask_erosion_m"] == 0.02
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


def test_tracking_rate_above_supported_current_limit_is_rejected(
    tmp_path: Path,
) -> None:
    provider = _load_provider_module()
    config_path = tmp_path / "tracker.json"
    config_path.write_text(
        json.dumps(
            {
                "policy_stream": "robot_arm.scene.segmentation_policy",
                "tracking_rate_hz": 4.1,
                "vlm_candidates": [{"backend": "test", "model": "test"}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="tracking_rate_hz"):
        provider.load_config(config_path)


def test_angular_count_above_controller_scene_limit_is_rejected(
    tmp_path: Path,
) -> None:
    provider = _load_provider_module()
    config_path = tmp_path / "tracker.json"
    config_path.write_text(
        json.dumps(
            {
                "policy_stream": "robot_arm.scene.segmentation_policy",
                "angular_direction_count": 20_001,
                "vlm_candidates": [{"backend": "test", "model": "test"}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="angular_direction_count"):
        provider.load_config(config_path)


def test_negative_metric_mask_erosion_is_rejected(tmp_path: Path) -> None:
    provider = _load_provider_module()
    config_path = tmp_path / "tracker.json"
    config_path.write_text(
        json.dumps(
            {
                "policy_stream": "robot_arm.scene.segmentation_policy",
                "work_object_mask_erosion_m": -0.001,
                "vlm_candidates": [{"backend": "test", "model": "test"}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="work_object_mask_erosion_m"):
        provider.load_config(config_path)
