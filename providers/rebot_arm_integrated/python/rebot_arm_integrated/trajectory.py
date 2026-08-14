from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import numpy as np


def _vector(values: Iterable[float], name: str) -> np.ndarray:
    result = np.asarray(list(values), dtype=float)
    if result.ndim != 1 or result.size == 0 or not np.all(np.isfinite(result)):
        raise ValueError(f"{name} must be a finite one-dimensional vector")
    return result


@dataclass(frozen=True)
class QuinticJointSegment:
    q0: np.ndarray
    qd0: np.ndarray
    qdd0: np.ndarray
    q1: np.ndarray
    qd1: np.ndarray
    qdd1: np.ndarray
    duration_s: float

    @classmethod
    def create(
        cls,
        q0: Iterable[float],
        q1: Iterable[float],
        duration_s: float,
        *,
        qd0: Iterable[float] | None = None,
        qdd0: Iterable[float] | None = None,
        qd1: Iterable[float] | None = None,
        qdd1: Iterable[float] | None = None,
    ) -> "QuinticJointSegment":
        start = _vector(q0, "q0")
        goal = _vector(q1, "q1")
        if start.shape != goal.shape:
            raise ValueError("q0 and q1 must have the same shape")
        zeros = np.zeros_like(start)

        def boundary(values: Iterable[float] | None, name: str) -> np.ndarray:
            result = zeros.copy() if values is None else _vector(values, name)
            if result.shape != start.shape:
                raise ValueError(f"{name} must match q0")
            return result

        duration = float(duration_s)
        if not np.isfinite(duration) or duration <= 0.0:
            raise ValueError("duration_s must be positive and finite")
        return cls(
            start,
            boundary(qd0, "qd0"),
            boundary(qdd0, "qdd0"),
            goal,
            boundary(qd1, "qd1"),
            boundary(qdd1, "qdd1"),
            duration,
        )

    def sample(self, elapsed_s: float) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
        t = float(np.clip(float(elapsed_s), 0.0, self.duration_s))
        duration = self.duration_s
        c0 = self.q0
        c1 = self.qd0
        c2 = self.qdd0 / 2.0
        delta = self.q1 - self.q0
        c3 = (
            20.0 * delta
            - (8.0 * self.qd1 + 12.0 * self.qd0) * duration
            - (3.0 * self.qdd0 - self.qdd1) * duration**2
        ) / (2.0 * duration**3)
        c4 = (
            -30.0 * delta
            + (14.0 * self.qd1 + 16.0 * self.qd0) * duration
            + (3.0 * self.qdd0 - 2.0 * self.qdd1) * duration**2
        ) / (2.0 * duration**4)
        c5 = (
            12.0 * delta
            - (6.0 * self.qd1 + 6.0 * self.qd0) * duration
            - (self.qdd0 - self.qdd1) * duration**2
        ) / (2.0 * duration**5)
        q = c0 + c1*t + c2*t**2 + c3*t**3 + c4*t**4 + c5*t**5
        qd = c1 + 2.0*c2*t + 3.0*c3*t**2 + 4.0*c4*t**3 + 5.0*c5*t**4
        qdd = 2.0*c2 + 6.0*c3*t + 12.0*c4*t**2 + 20.0*c5*t**3
        return q, qd, qdd, t / duration

    def sampled(self, count: int) -> list[tuple[np.ndarray, np.ndarray, np.ndarray, float]]:
        sample_count = int(count)
        if sample_count < 2:
            raise ValueError("count must be at least two")
        return [self.sample(value) for value in np.linspace(0.0, self.duration_s, sample_count)]


@dataclass(frozen=True)
class TimedJointPath:
    """Piecewise-linear joint path with an explicit duration for every leg."""

    q_waypoints: np.ndarray
    stage_durations_s: np.ndarray
    cumulative_times_s: np.ndarray

    @classmethod
    def create(
        cls,
        q_waypoints: Iterable[Iterable[float]],
        stage_durations_s: Iterable[float],
    ) -> "TimedJointPath":
        waypoints = np.asarray(
            [list(values) for values in q_waypoints],
            dtype=float,
        )
        durations = np.asarray(list(stage_durations_s), dtype=float)
        if (
            waypoints.ndim != 2
            or waypoints.shape[0] < 2
            or waypoints.shape[1] == 0
            or not np.all(np.isfinite(waypoints))
        ):
            raise ValueError(
                "q_waypoints must contain at least two matching finite vectors"
            )
        if (
            durations.shape != (waypoints.shape[0] - 1,)
            or not np.all(np.isfinite(durations))
            or np.any(durations <= 0.0)
        ):
            raise ValueError(
                "stage_durations_s must contain one positive finite value per leg"
            )
        cumulative = np.concatenate(
            [np.zeros(1, dtype=float), np.cumsum(durations)]
        )
        return cls(waypoints, durations, cumulative)

    @property
    def duration_s(self) -> float:
        return float(self.cumulative_times_s[-1])

    def sample(
        self,
        elapsed_s: float,
    ) -> tuple[np.ndarray, np.ndarray, int, float]:
        """Return position, velocity, one-based leg index, and path progress."""

        elapsed = float(elapsed_s)
        if not np.isfinite(elapsed):
            raise ValueError("elapsed_s must be finite")
        if elapsed <= 0.0:
            stage_index = 0
            stage_elapsed = 0.0
        elif elapsed >= self.duration_s:
            return (
                self.q_waypoints[-1].copy(),
                np.zeros(self.q_waypoints.shape[1], dtype=float),
                len(self.stage_durations_s),
                1.0,
            )
        else:
            stage_index = int(
                np.searchsorted(
                    self.cumulative_times_s,
                    elapsed,
                    side="right",
                )
                - 1
            )
            stage_index = min(stage_index, len(self.stage_durations_s) - 1)
            stage_elapsed = elapsed - float(
                self.cumulative_times_s[stage_index]
            )
        duration = float(self.stage_durations_s[stage_index])
        alpha = float(np.clip(stage_elapsed / duration, 0.0, 1.0))
        q_start = self.q_waypoints[stage_index]
        delta = self.q_waypoints[stage_index + 1] - q_start
        return (
            q_start + alpha * delta,
            delta / duration,
            stage_index + 1,
            float(np.clip(elapsed, 0.0, self.duration_s) / self.duration_s),
        )
