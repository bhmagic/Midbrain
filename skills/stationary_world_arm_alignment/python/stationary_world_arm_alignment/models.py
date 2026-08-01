from __future__ import annotations

import copy
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class SkillState(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class RunMode(StrEnum):
    AUTO = "auto"
    FOUNDATION_BASE_VLM_GRIPPER = "foundation_base_vlm_gripper"
    FOUNDATION_BASE_GRIPPER = "foundation_base_gripper"
    VLM_GRIPPER_ONLY = "vlm_gripper_only"

    @classmethod
    def _missing_(cls, value: object):
        if value == "vlm_refine":
            return cls.VLM_GRIPPER_ONLY
        return None


PUBLIC_RUN_MODES = (
    RunMode.AUTO,
    RunMode.FOUNDATION_BASE_VLM_GRIPPER,
    RunMode.FOUNDATION_BASE_GRIPPER,
    RunMode.VLM_GRIPPER_ONLY,
)


def canonical_run_mode(mode: RunMode) -> RunMode:
    return RunMode(mode)


MODE_CONTRACTS: dict[str, dict[str, Any]] = {
    str(RunMode.FOUNDATION_BASE_VLM_GRIPPER): {
        "base_alignment_source": "FOUNDATIONPOSE_BASE_POSE",
        "gripper_alignment_source": "VLM_GRIPPER_RGBD_BASE_X_RELATION",
        "foundation_pose_models": ["base"],
        "requires_prior_alignment": False,
    },
    str(RunMode.FOUNDATION_BASE_GRIPPER): {
        "base_alignment_source": "FOUNDATIONPOSE_BASE_POSE",
        "gripper_alignment_source": "FOUNDATIONPOSE_GRIPPER_AUXILIARY_ONLY",
        "foundation_pose_models": ["base", "gripper"],
        "requires_prior_alignment": False,
    },
    str(RunMode.VLM_GRIPPER_ONLY): {
        "base_alignment_source": "PRIOR_ALIGNMENT_LOCKED_ROTATION",
        "gripper_alignment_source": "VLM_RGBD_BEAK",
        "foundation_pose_models": [],
        "requires_prior_alignment": True,
    },
}


def mode_contract(mode: RunMode | str) -> dict[str, Any]:
    canonical = canonical_run_mode(RunMode(mode))
    if canonical == RunMode.AUTO:
        raise ValueError("auto has no fixed measurement-source contract")
    return copy.deepcopy(MODE_CONTRACTS[str(canonical)])


@dataclass
class Progress:
    skill_id: str = ""
    alignment_id: str = ""
    mode: str = ""
    state: str = SkillState.PENDING
    phase: str = "IDLE"
    message: str = "No alignment is running."
    started_at_us: int | None = None
    updated_at_us: int = field(default_factory=lambda: time.time_ns() // 1000)
    elapsed_s: float = 0.0
    progress_kind: str = "milestone"
    completed_units: int = 0
    total_units: int = 0
    provider_responsive: bool | None = None
    provider_sessions: list[dict[str, Any]] = field(default_factory=list)
    selected_providers: dict[str, str] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] | None = None

    def snapshot(self) -> dict[str, Any]:
        value = copy.deepcopy(self.__dict__)
        value["state"] = str(self.state)
        return value
