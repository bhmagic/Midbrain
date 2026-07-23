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
