from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import numpy as np

from .modes import BASIC_POS_TOR, BASIC_POS_VEL


def _six(values: Iterable[float], name: str) -> np.ndarray:
    result = np.asarray(list(values), dtype=float)
    if result.shape != (6,) or not np.all(np.isfinite(result)):
        raise ValueError(f"{name} must contain six finite values")
    return result


def synchronized_velocity_limits(
    q_start: Iterable[float],
    q_goal: Iterable[float],
    duration_s: float,
    provider_caps_rad_s: Iterable[float],
    *,
    stationary_joint_limit_rad_s: float,
) -> np.ndarray:
    start = _six(q_start, "q_start")
    goal = _six(q_goal, "q_goal")
    caps = _six(provider_caps_rad_s, "provider_caps_rad_s")
    duration = float(duration_s)
    stationary = float(stationary_joint_limit_rad_s)
    if duration <= 0.0 or not np.isfinite(duration):
        raise ValueError("duration_s must be positive and finite")
    if np.any(caps <= 0.0) or stationary <= 0.0:
        raise ValueError("velocity limits must be positive")
    limits = np.maximum(np.abs(goal - start) / duration, stationary)
    return np.minimum(limits, caps)


@dataclass
class LatchedEndpointCommand:
    basic_mode: str
    q_start: np.ndarray
    q_goal: np.ndarray
    velocity_limits_rad_s: np.ndarray
    keepalive_period_s: float
    torque_limit_ratios: np.ndarray | None = None
    last_sent_monotonic: float = 0.0
    send_count: int = 0

    @classmethod
    def create(
        cls,
        basic_mode: str,
        q_start: Iterable[float],
        q_goal: Iterable[float],
        velocity_limits_rad_s: Iterable[float],
        *,
        keepalive_period_s: float,
        torque_limit_ratios: Iterable[float] | None = None,
    ) -> "LatchedEndpointCommand":
        if basic_mode not in {BASIC_POS_VEL, BASIC_POS_TOR}:
            raise ValueError("latched endpoint commands are only valid for POS_VEL or POS_TOR")
        start = _six(q_start, "q_start")
        goal = _six(q_goal, "q_goal")
        limits = _six(velocity_limits_rad_s, "velocity_limits_rad_s")
        if np.any(limits <= 0.0):
            raise ValueError("velocity limits must be positive")
        keepalive = float(keepalive_period_s)
        if keepalive <= 0.0 or not np.isfinite(keepalive):
            raise ValueError("keepalive_period_s must be positive and finite")
        ratios = None if torque_limit_ratios is None else _six(torque_limit_ratios, "torque_limit_ratios")
        if basic_mode == BASIC_POS_TOR:
            if ratios is None or np.any(ratios <= 0.0) or np.any(ratios > 1.0):
                raise ValueError("POS_TOR requires six explicit torque ratios in (0, 1]")
        elif ratios is not None:
            raise ValueError("POS_VEL must not carry torque ratios")
        return cls(basic_mode, start, goal, limits, keepalive, ratios)

    def should_send(self, now_monotonic: float) -> bool:
        now = float(now_monotonic)
        return self.send_count == 0 or now - self.last_sent_monotonic >= self.keepalive_period_s

    def mark_sent(self, now_monotonic: float) -> None:
        self.last_sent_monotonic = float(now_monotonic)
        self.send_count += 1

    def commands(self) -> list[dict]:
        result = []
        for index in range(6):
            values = {
                "position_rad": float(self.q_goal[index]),
                "velocity_limit_rad_s": float(self.velocity_limits_rad_s[index]),
            }
            if self.basic_mode == BASIC_POS_TOR:
                values["torque_limit_ratio"] = float(self.torque_limit_ratios[index])
            result.append({"joint_index": index, "mode": self.basic_mode, "values": values})
        return result

    def arrived(
        self,
        measured_q: Iterable[float],
        measured_qd: Iterable[float],
        *,
        position_tolerance_rad: Iterable[float],
        velocity_tolerance_rad_s: Iterable[float],
    ) -> bool:
        q = _six(measured_q, "measured_q")
        qd = _six(measured_qd, "measured_qd")
        q_tol = _six(position_tolerance_rad, "position_tolerance_rad")
        qd_tol = _six(velocity_tolerance_rad_s, "velocity_tolerance_rad_s")
        if np.any(q_tol <= 0.0) or np.any(qd_tol <= 0.0):
            raise ValueError("arrival tolerances must be positive")
        return bool(np.all(np.abs(q - self.q_goal) <= q_tol) and np.all(np.abs(qd) <= qd_tol))

    def snapshot(self) -> dict:
        return {
            "strategy": "LATCHED_ENDPOINT_KEEPALIVE",
            "basic_mode": self.basic_mode,
            "q_start": self.q_start.tolist(),
            "q_goal": self.q_goal.tolist(),
            "velocity_limits_rad_s": self.velocity_limits_rad_s.tolist(),
            "torque_limit_ratios": None if self.torque_limit_ratios is None else self.torque_limit_ratios.tolist(),
            "keepalive_period_s": self.keepalive_period_s,
            "send_count": self.send_count,
        }
