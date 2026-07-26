from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


def skill_root() -> Path:
    return Path(__file__).resolve().parents[2]


def workspace_root() -> Path:
    configured = os.getenv("PHYSICAL_AGENT_ROOT")
    if configured:
        return Path(configured).resolve()
    return skill_root().parent.parent


SKILL_ROOT = skill_root()
WORKSPACE_ROOT = workspace_root()
load_dotenv(WORKSPACE_ROOT / "config" / "api_keys.env", override=False)
load_dotenv(WORKSPACE_ROOT / "config" / "system.env", override=False)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_skill_config(path: Path | None = None) -> dict[str, Any]:
    template = SKILL_ROOT / "config_templates" / "alignment.default.json"
    config = json.loads(template.read_text(encoding="utf-8"))
    local_config = SKILL_ROOT / "config" / "alignment.json"
    legacy_config = (
        WORKSPACE_ROOT
        / "config"
        / "skills"
        / "stationary_world_arm_alignment"
        / "alignment.json"
    )
    configured = path or (local_config if local_config.is_file() else legacy_config)
    if configured.is_file():
        config = _deep_merge(
            config,
            json.loads(configured.read_text(encoding="utf-8")),
        )
    validation = config["pose_validation"]
    strict_confidence = float(validation["minimum_confidence"])
    fallback_confidence = float(
        validation["best_of_two_fallback_minimum_confidence"]
    )
    if not 0.0 < fallback_confidence <= strict_confidence <= 1.0:
        raise ValueError(
            "pose-validation confidence thresholds must satisfy "
            "0 < fallback <= strict <= 1"
        )
    return config


@dataclass(frozen=True)
class Settings:
    manager_url: str = os.getenv("MANAGER_URL", "http://127.0.0.1:7001")
    fabric_url: str = os.getenv("FABRIC_URL", "http://127.0.0.1:7002")
    foundation_pose_control_url: str = os.getenv(
        "FOUNDATION_POSE_CONTROL_URL",
        "http://127.0.0.1:7103",
    )
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_vision_model: str = os.getenv("OPENAI_VISION_MODEL", "gpt-5.6-luna")

    @property
    def run_root(self) -> Path:
        path = SKILL_ROOT / "run"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def calibration_root(self) -> Path:
        path = SKILL_ROOT / "config" / "calibrations"
        path.mkdir(parents=True, exist_ok=True)
        return path
