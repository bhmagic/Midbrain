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
    maximum_attempts = validation.get("maximum_attempts")
    if isinstance(maximum_attempts, bool) or maximum_attempts != 2:
        raise ValueError(
            "pose-validation maximum_attempts must be exactly 2"
        )
    maximum_size_mismatch = float(
        validation.get("maximum_projected_box_size_mismatch_fraction")
        or 0.0
    )
    if not 0.0 < maximum_size_mismatch <= 0.25:
        raise ValueError(
            "projected-box size mismatch must be in (0, 0.25]"
        )
    axis_length_m = float(validation.get("axis_length_m") or 0.0)
    if not 0.0 < axis_length_m <= 1.0:
        raise ValueError("pose-validation axis_length_m must be in (0, 1]")
    from .foundation_engine import normalize_base_pose_engine_route

    normalize_base_pose_engine_route(config)
    candidate_review = config.get("candidate_review") or {}
    candidate_mode = str(
        candidate_review.get("mode") or "SHADOW"
    ).upper()
    if candidate_mode not in {"SHADOW", "ENFORCED"}:
        raise ValueError(
            "candidate-review mode must be SHADOW or ENFORCED"
        )
    if float(candidate_review.get("ttl_s") or 0) <= 0:
        raise ValueError("candidate-review TTL must be positive")
    base_alignment = config.get("base_alignment") or {}
    base_up_warning_tilt_deg = float(
        base_alignment.get("base_up_warning_tilt_deg") or 0.0
    )
    if not 0.0 < base_up_warning_tilt_deg <= 90.0:
        raise ValueError("base_up_warning_tilt_deg must be in (0, 90]")
    maximum_learned_offset_m = float(
        (config.get("tool_geometry") or {}).get(
            "maximum_learned_tool_to_beak_norm_m"
        )
        or 0.0
    )
    if not 0.0 < maximum_learned_offset_m <= 0.5:
        raise ValueError(
            "maximum_learned_tool_to_beak_norm_m must be in (0, 0.5]"
        )
    vlm_translation_bound = float(
        (config.get("vlm_refine") or {}).get(
            "single_observation_translation_error_bound_m",
            0.0,
        )
    )
    if not 0.0 < vlm_translation_bound <= 0.01:
        raise ValueError(
            "VLM-only translation error bound must be in (0, 0.01]"
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

    @property
    def review_root(self) -> Path:
        path = SKILL_ROOT / "config" / "calibration_reviews"
        path.mkdir(parents=True, exist_ok=True)
        return path
