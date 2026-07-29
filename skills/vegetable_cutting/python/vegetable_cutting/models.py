from __future__ import annotations

import copy
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class SkillState(StrEnum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    WAITING_FOR_OPERATOR = "WAITING_FOR_OPERATOR"
    READY_FOR_OPERATOR_TAKEOVER = "READY_FOR_OPERATOR_TAKEOVER"
    COMPLETED = "COMPLETED"
    ABORTED = "ABORTED"
    FAILED = "FAILED"


class Phase(StrEnum):
    IDLE = "IDLE"
    PREFLIGHT = "PREFLIGHT"
    WAIT_TOOL_LOAD = "WAIT_TOOL_LOAD"
    WAIT_WORKPIECE_LOAD = "WAIT_WORKPIECE_LOAD"
    PERCEIVING = "PERCEIVING"
    PLANNING = "PLANNING"
    READY_FOR_OPERATOR_TAKEOVER = "READY_FOR_OPERATOR_TAKEOVER"
    TRANSFER_TO_FIRST_CUT = "TRANSFER_TO_FIRST_CUT"
    WAIT_FIRST_CUT_CONFIRMATION = "WAIT_FIRST_CUT_CONFIRMATION"
    CUTTING = "CUTTING"
    TRACKING_CHECK = "TRACKING_CHECK"
    WAIT_TOOL_REMOVAL = "WAIT_TOOL_REMOVAL"
    SAFE_TERMINATING = "SAFE_TERMINATING"
    ABORTED = "ABORTED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class RunParameters:
    slice_spacing_mm: float
    blade_yaw_deg: float = 0.0
    maximum_cut_count: int = 40

    def validated(self) -> "RunParameters":
        if not 1.0 <= float(self.slice_spacing_mm) <= 200.0:
            raise ValueError("slice_spacing_mm must be in [1, 200]")
        if not -180.0 <= float(self.blade_yaw_deg) <= 180.0:
            raise ValueError("blade_yaw_deg must be in [-180, 180]")
        if not 1 <= int(self.maximum_cut_count) <= 100:
            raise ValueError("maximum_cut_count must be in [1, 100]")
        return self


@dataclass
class Progress:
    skill_id: str = ""
    plan_id: str = ""
    state: str = SkillState.IDLE
    phase: str = Phase.IDLE
    message: str = "No cutting plan is active."
    started_at_us: int | None = None
    updated_at_us: int = field(default_factory=lambda: time.time_ns() // 1000)
    operator_tool_loaded: bool = False
    operator_tool_attachment_confirmed: bool = False
    operator_workpiece_loaded: bool = False
    operator_outside_workspace: bool = False
    motion_submission_enabled: bool = False
    motion_submitted: bool = False
    provider_readiness: dict[str, Any] = field(default_factory=dict)
    alignment: dict[str, Any] | None = None
    tracking: dict[str, Any] | None = None
    execution: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    error: str | None = None

    def snapshot(self) -> dict[str, Any]:
        value = copy.deepcopy(self.__dict__)
        value["state"] = str(self.state)
        value["phase"] = str(self.phase)
        return value
