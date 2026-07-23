from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import numpy as np


POS_VEL_APPROACH = "POS_VEL_APPROACH"
MIT_SETTLE = "MIT_SETTLE"
COMPLETE = "COMPLETE"


def _six(values: Iterable[float], name: str) -> np.ndarray:
    result = np.asarray(list(values), dtype=float)
    if result.shape != (6,) or not np.all(np.isfinite(result)):
        raise ValueError(f"{name} must contain six finite values")
    return result


@dataclass
class HybridApproachPolicy:
    """One-way POS_VEL to MIT handoff policy; it never oscillates between modes."""

    q_goal: np.ndarray
    handoff_position_error_rad: np.ndarray
    handoff_velocity_rad_s: np.ndarray
    completion_position_error_rad: np.ndarray
    completion_velocity_rad_s: np.ndarray
    required_stable_samples: int
    phase: str = POS_VEL_APPROACH
    stable_samples: int = 0

    @classmethod
    def create(
        cls,
        q_goal: Iterable[float],
        *,
        handoff_position_error_rad: Iterable[float],
        handoff_velocity_rad_s: Iterable[float],
        completion_position_error_rad: Iterable[float],
        completion_velocity_rad_s: Iterable[float],
        required_stable_samples: int,
    ) -> "HybridApproachPolicy":
        policy = cls(
            _six(q_goal, "q_goal"),
            _six(handoff_position_error_rad, "handoff_position_error_rad"),
            _six(handoff_velocity_rad_s, "handoff_velocity_rad_s"),
            _six(completion_position_error_rad, "completion_position_error_rad"),
            _six(completion_velocity_rad_s, "completion_velocity_rad_s"),
            int(required_stable_samples),
        )
        for values in (
            policy.handoff_position_error_rad,
            policy.handoff_velocity_rad_s,
            policy.completion_position_error_rad,
            policy.completion_velocity_rad_s,
        ):
            if np.any(values <= 0.0):
                raise ValueError("hybrid handoff thresholds must be positive")
        if policy.required_stable_samples < 2:
            raise ValueError("hybrid handoff requires at least two stable samples")
        return policy

    def update(self, measured_q: Iterable[float], measured_qd: Iterable[float]) -> str:
        q = _six(measured_q, "measured_q")
        qd = _six(measured_qd, "measured_qd")
        if self.phase == COMPLETE:
            return self.phase
        if self.phase == POS_VEL_APPROACH:
            within = bool(
                np.all(np.abs(q - self.q_goal) <= self.handoff_position_error_rad)
                and np.all(np.abs(qd) <= self.handoff_velocity_rad_s)
            )
        else:
            within = bool(
                np.all(np.abs(q - self.q_goal) <= self.completion_position_error_rad)
                and np.all(np.abs(qd) <= self.completion_velocity_rad_s)
            )
        self.stable_samples = self.stable_samples + 1 if within else 0
        if self.stable_samples >= self.required_stable_samples:
            self.phase = MIT_SETTLE if self.phase == POS_VEL_APPROACH else COMPLETE
            self.stable_samples = 0
        return self.phase

    def snapshot(self) -> dict:
        return {
            "phase": self.phase,
            "stable_samples": self.stable_samples,
            "required_stable_samples": self.required_stable_samples,
            "q_goal": self.q_goal.tolist(),
            "one_way_handoff": True,
            "physical_execution_enabled": False,
        }
