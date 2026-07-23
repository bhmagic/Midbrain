"""Calibration experiment recording and robust first-order regression."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import json
import time
import uuid

import numpy as np


@dataclass
class FitResult:
    gravity_scale: float
    gravity_phase_offset_rad: float
    effective_inertia: float
    coulomb_friction_positive_nm: float
    coulomb_friction_negative_nm: float
    viscous_friction: float
    torque_bias: float
    breakaway_friction_positive_nm: float
    breakaway_friction_negative_nm: float
    training_rms_residual: float
    validation_rms_residual: float
    validation_max_residual: float
    condition_number: float
    sample_count: int
    static_sample_count: int
    gravity_excitation_nm: float
    gravity_gradient_excitation_nm_per_rad: float
    acceleration_excitation_rad_s2: float
    velocity_excitation_rad_s: float
    gravity_identifiable: bool
    gravity_phase_identifiable: bool
    inertia_identifiable: bool
    friction_identifiable: bool
    breakaway_identifiable: bool
    accepted: bool

    @property
    def coulomb_friction(self) -> float:
        return 0.5 * (self.coulomb_friction_positive_nm + self.coulomb_friction_negative_nm)

    @property
    def rms_residual(self) -> float:
        return self.validation_rms_residual

    @property
    def max_residual(self) -> float:
        return self.validation_max_residual

    def to_dict(self) -> dict[str, Any]:
        result = self.__dict__.copy()
        # Compatibility names used by earlier GUI and calibration revisions.
        result["coulomb_friction"] = self.coulomb_friction
        result["rms_residual"] = self.validation_rms_residual
        result["max_residual"] = self.validation_max_residual
        return result


def smooth_derivatives(time_s: np.ndarray, position: np.ndarray, window: int = 11) -> tuple[np.ndarray, np.ndarray]:
    """Estimate velocity and acceleration using local quadratic fits."""
    if len(position) < 5:
        velocity = np.gradient(position, time_s)
        return velocity, np.gradient(velocity, time_s)
    window = max(5, window | 1)
    half = window // 2
    velocity = np.zeros_like(position)
    acceleration = np.zeros_like(position)
    for i in range(len(position)):
        lo = max(0, i - half)
        hi = min(len(position), i + half + 1)
        t = time_s[lo:hi] - time_s[i]
        degree = min(2, len(t) - 1)
        coefficients = np.polyfit(t, position[lo:hi], degree)
        if degree == 2:
            velocity[i] = coefficients[-2]
            acceleration[i] = 2 * coefficients[-3]
        else:
            velocity[i] = coefficients[0]
            acceleration[i] = 0.0
    return velocity, acceleration


def _robust_solve(x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray, float]:
    """Huber-style iteratively reweighted least squares with column scaling."""
    scales = np.std(x, axis=0)
    scales = np.where(scales > 1e-9, scales, 1.0)
    xs = x / scales
    weights = np.ones(len(y))
    theta_scaled = np.zeros(xs.shape[1])
    for _ in range(12):
        wx = xs * weights[:, None]
        theta_scaled = np.linalg.lstsq(wx, y * weights, rcond=None)[0]
        residual = y - xs @ theta_scaled
        scale = max(1.4826 * np.median(np.abs(residual - np.median(residual))), 1e-6)
        normalized = np.abs(residual) / (1.345 * scale)
        weights = np.where(normalized <= 1.0, 1.0, 1.0 / normalized)
    theta = theta_scaled / scales
    residual = y - x @ theta
    condition = float(np.linalg.cond(xs))
    return theta, residual, condition


def _percentile_or_nan(values: np.ndarray, percentile: float) -> float:
    if len(values) < 5:
        return float("nan")
    return float(np.percentile(values, percentile))


def robust_fit(samples: list[dict[str, Any]], include_inertia: bool = True, smoothing_velocity: float = 0.03) -> FitResult:
    """Fit a richer single-joint effective model.

    The fitted model is:

      tau = kg*g_nom + kphase*dg_dq + Jeff*qdd
            + Fc_pos*max(tanh(qd/vs), 0)
            + Fc_neg*min(tanh(qd/vs), 0)
            + b*qd + bias

    The small-angle gravity phase estimate is kphase/kg.  It is a gravity-model
    correction only; it does not alter the kinematic encoder zero.
    """
    if len(samples) < 80:
        raise ValueError("at least 80 samples are required")

    t = np.asarray([s["time_s"] for s in samples], dtype=float)
    q = np.asarray([s["position_rad"] for s in samples], dtype=float)
    qd = np.asarray([s.get("velocity_rad_s", np.nan) for s in samples], dtype=float)
    qdd = np.asarray([s.get("acceleration_rad_s2", np.nan) for s in samples], dtype=float)
    if not np.all(np.isfinite(qd)) or (include_inertia and not np.all(np.isfinite(qdd))):
        qd, qdd = smooth_derivatives(t, q)

    nominal = np.asarray([s["nominal_gravity_nm"] for s in samples], dtype=float)
    gravity_gradient = np.asarray([s.get("gravity_gradient_nm_per_rad", 0.0) for s in samples], dtype=float)
    torque = np.asarray([s["measured_torque_nm"] for s in samples], dtype=float)

    smooth_sign = np.tanh(qd / max(smoothing_velocity, 1e-4))
    positive_coulomb = np.maximum(smooth_sign, 0.0)
    negative_coulomb = np.minimum(smooth_sign, 0.0)

    use_gravity_phase = bool(np.std(gravity_gradient) > 1e-6)
    columns = [nominal]
    if use_gravity_phase:
        columns.append(gravity_gradient)
    if include_inertia:
        columns.append(qdd)
    columns.extend([positive_coulomb, negative_coulomb, qd, np.ones_like(qd)])
    x = np.column_stack(columns)

    # Keep complete time neighborhoods out of validation by assigning every
    # fifth block, rather than every fifth point, to reduce leakage.
    block = np.floor((t - t[0]) / 0.5).astype(int)
    validate = block % 5 == 0
    if np.count_nonzero(validate) < 20 or np.count_nonzero(~validate) < 50:
        indices = np.arange(len(samples))
        validate = indices % 5 == 0
    train = ~validate

    theta, training_residual, condition = _robust_solve(x[train], torque[train])
    validation_residual = torque[validate] - x[validate] @ theta

    cursor = 0
    gravity_scale = float(theta[cursor]); cursor += 1
    if use_gravity_phase:
        gravity_phase_coefficient = float(theta[cursor]); cursor += 1
    else:
        gravity_phase_coefficient = 0.0
    if include_inertia:
        inertia = float(theta[cursor]); cursor += 1
    else:
        inertia = 0.0
    coulomb_positive = float(theta[cursor]); cursor += 1
    coulomb_negative = float(theta[cursor]); cursor += 1
    viscous = float(theta[cursor]); cursor += 1
    bias = float(theta[cursor])

    gravity_phase_offset = gravity_phase_coefficient / gravity_scale if abs(gravity_scale) > 1e-6 else 0.0

    train_rms = float(np.sqrt(np.mean(training_residual ** 2)))
    validation_rms = float(np.sqrt(np.mean(validation_residual ** 2)))
    validation_max = float(np.max(np.abs(validation_residual)))

    gravity_excitation = float(np.ptp(nominal))
    gravity_gradient_excitation = float(np.ptp(gravity_gradient))
    acceleration_excitation = float(np.ptp(qdd))
    velocity_excitation = float(np.ptp(qd))

    gravity_identifiable = bool(gravity_excitation >= 0.15 and np.std(nominal) >= 0.04)
    gravity_phase_identifiable = bool(
        gravity_identifiable
        and gravity_gradient_excitation >= 0.15
        and np.std(gravity_gradient) >= 0.04
        and abs(gravity_phase_offset) <= 0.25
    )
    inertia_identifiable = bool(include_inertia and acceleration_excitation >= 0.35 and np.std(qdd) >= 0.08)
    friction_identifiable = bool(np.min(qd) < -0.05 and np.max(qd) > 0.05 and velocity_excitation >= 0.20)

    # Breakaway friction is a diagnostic estimate from low-speed reversal data.
    phase_term = gravity_phase_coefficient * gravity_gradient
    base_without_coulomb = gravity_scale * nominal + phase_term + inertia * qdd + viscous * qd + bias
    reversal = (np.abs(qd) < 0.04) & (np.abs(qdd) > 0.08)
    static_residual = torque - base_without_coulomb
    positive_breakaway_values = static_residual[reversal & (qdd > 0)]
    negative_breakaway_values = -static_residual[reversal & (qdd < 0)]
    breakaway_positive = _percentile_or_nan(positive_breakaway_values, 90.0)
    breakaway_negative = _percentile_or_nan(negative_breakaway_values, 90.0)
    static_sample_count = int(np.count_nonzero(reversal))
    breakaway_identifiable = bool(np.isfinite(breakaway_positive) and np.isfinite(breakaway_negative) and static_sample_count >= 20)

    residual_limit = max(0.35, 0.20 * float(np.std(torque)) + 0.05)
    physically_plausible = (
        0.25 <= gravity_scale <= 2.5
        and inertia >= -0.02
        and coulomb_positive >= -0.05
        and coulomb_negative >= -0.05
        and viscous >= -0.05
    )
    accepted = bool(
        np.isfinite(condition)
        and condition < 1e5
        and validation_rms < residual_limit
        and friction_identifiable
        and physically_plausible
    )

    return FitResult(
        gravity_scale=gravity_scale,
        gravity_phase_offset_rad=float(gravity_phase_offset),
        effective_inertia=inertia,
        coulomb_friction_positive_nm=coulomb_positive,
        coulomb_friction_negative_nm=coulomb_negative,
        viscous_friction=viscous,
        torque_bias=bias,
        breakaway_friction_positive_nm=breakaway_positive,
        breakaway_friction_negative_nm=breakaway_negative,
        training_rms_residual=train_rms,
        validation_rms_residual=validation_rms,
        validation_max_residual=validation_max,
        condition_number=condition,
        sample_count=len(samples),
        static_sample_count=static_sample_count,
        gravity_excitation_nm=gravity_excitation,
        gravity_gradient_excitation_nm_per_rad=gravity_gradient_excitation,
        acceleration_excitation_rad_s2=acceleration_excitation,
        velocity_excitation_rad_s=velocity_excitation,
        gravity_identifiable=gravity_identifiable,
        gravity_phase_identifiable=gravity_phase_identifiable,
        inertia_identifiable=inertia_identifiable,
        friction_identifiable=friction_identifiable,
        breakaway_identifiable=breakaway_identifiable,
        accepted=accepted,
    )


class SessionRecorder:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def start(self, metadata: dict[str, Any], write_metadata: bool = True) -> tuple[str, Path]:
        session_id = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:8]}"
        path = self.root / session_id
        path.mkdir(parents=True)
        if write_metadata:
            (path / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        return session_id, path

    @staticmethod
    def write_samples(path: Path, samples: list[dict[str, Any]]) -> None:
        with (path / "samples.jsonl").open("w", encoding="utf-8") as stream:
            for sample in samples:
                stream.write(json.dumps(sample, separators=(",", ":")) + "\n")

    @staticmethod
    def write_result(path: Path, result: dict[str, Any]) -> None:
        (path / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


@dataclass
class FrictionFitResult:
    """Two-parameter joint-friction fit with the factory gravity model retained."""

    coulomb_friction_nm: float
    viscous_friction_nm_per_rad_s: float
    training_rms_residual_nm: float
    validation_rms_residual_nm: float
    validation_max_residual_nm: float
    condition_number: float
    pair_count: int
    slow_pair_count: int
    fast_pair_count: int
    slow_speed_rad_s: float
    fast_speed_rad_s: float
    friction_identifiable: bool
    factory_gravity_retained: bool
    accepted: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.__dict__,
            # Compatibility fields consumed by the calibration configuration.
            "coulomb_friction": self.coulomb_friction_nm,
            "coulomb_friction_positive_nm": self.coulomb_friction_nm,
            "coulomb_friction_negative_nm": self.coulomb_friction_nm,
            "viscous_friction": self.viscous_friction_nm_per_rad_s,
            "gravity_identifiable": False,
            "gravity_phase_identifiable": False,
            "inertia_identifiable": False,
            "breakaway_identifiable": False,
            "friction_identifiable": self.friction_identifiable,
            "gravity_scale": 1.0,
            "gravity_phase_offset_rad": 0.0,
            "effective_inertia": 0.0,
            "torque_bias": 0.0,
            "training_rms_residual": self.training_rms_residual_nm,
            "validation_rms_residual": self.validation_rms_residual_nm,
            "validation_max_residual": self.validation_max_residual_nm,
            "rms_residual": self.validation_rms_residual_nm,
            "max_residual": self.validation_max_residual_nm,
            "sample_count": self.pair_count,
        }


def _phase_pairs(samples: list[dict[str, Any]], phase_prefix: str, bins: int = 12) -> list[tuple[float, float]]:
    """Build gravity-cancelling forward/reverse torque pairs.

    At approximately the same joint angle and speed magnitude:

      0.5 * (tau_forward - tau_reverse) = Fc + b * |qdot|

    Factory gravity torque, static torque bias, and most position-dependent
    model error cancel in the difference.
    """
    positive = [s for s in samples if s.get("phase") == f"{phase_prefix}_positive"]
    negative = [s for s in samples if s.get("phase") == f"{phase_prefix}_negative"]
    if not positive or not negative:
        return []

    commanded_speed = max(
        float(np.median([abs(float(s.get("commanded_speed_rad_s", 0.0))) for s in positive + negative])),
        1e-3,
    )
    minimum = max(min(float(s["position_rad"]) for s in positive), min(float(s["position_rad"]) for s in negative))
    maximum = min(max(float(s["position_rad"]) for s in positive), max(float(s["position_rad"]) for s in negative))
    if maximum - minimum < 1e-3:
        return []

    edges = np.linspace(minimum, maximum, bins + 1)
    pairs: list[tuple[float, float]] = []
    for low, high in zip(edges[:-1], edges[1:]):
        p = [s for s in positive if low <= float(s["position_rad"]) < high and float(s["velocity_rad_s"]) > 0.55 * commanded_speed]
        n = [s for s in negative if low <= float(s["position_rad"]) < high and float(s["velocity_rad_s"]) < -0.55 * commanded_speed]
        if len(p) < 1 or len(n) < 1:
            continue
        tau_positive = float(np.median([float(s["measured_torque_nm"]) for s in p]))
        tau_negative = float(np.median([float(s["measured_torque_nm"]) for s in n]))
        speed = 0.5 * (
            float(np.median([abs(float(s["velocity_rad_s"])) for s in p]))
            + float(np.median([abs(float(s["velocity_rad_s"])) for s in n]))
        )
        friction_torque = 0.5 * (tau_positive - tau_negative)
        if np.isfinite(speed) and np.isfinite(friction_torque):
            pairs.append((speed, friction_torque))
    return pairs


def fit_two_parameter_friction(samples: list[dict[str, Any]]) -> FrictionFitResult:
    """Estimate symmetric Coulomb and viscous friction only.

    The factory link masses, centers of mass, inertias, and gravity model are
    deliberately not modified by this fit.
    """
    slow_pairs = _phase_pairs(samples, "friction_slow")
    fast_pairs = _phase_pairs(samples, "friction_fast")
    pairs = slow_pairs + fast_pairs
    if len(slow_pairs) < 5 or len(fast_pairs) < 5 or len(pairs) < 12:
        raise ValueError("insufficient steady forward/reverse samples for two-speed friction calibration")

    x = np.asarray([[1.0, speed] for speed, _ in pairs], dtype=float)
    y = np.asarray([torque for _, torque in pairs], dtype=float)

    # Spatially interleave train and validation pairs so both speed levels are
    # represented without using adjacent raw time samples in both sets.
    indices = np.arange(len(pairs))
    validation = indices % 4 == 0
    if np.count_nonzero(validation) < 4:
        validation = indices % 3 == 0
    training = ~validation

    theta, training_residual, condition = _robust_solve(x[training], y[training])
    validation_residual = y[validation] - x[validation] @ theta
    coulomb = float(theta[0])
    viscous = float(theta[1])
    training_rms = float(np.sqrt(np.mean(training_residual ** 2)))
    validation_rms = float(np.sqrt(np.mean(validation_residual ** 2)))
    validation_max = float(np.max(np.abs(validation_residual)))
    slow_speed = float(np.median([speed for speed, _ in slow_pairs]))
    fast_speed = float(np.median([speed for speed, _ in fast_pairs]))

    speed_separation = fast_speed - slow_speed
    friction_identifiable = bool(
        len(slow_pairs) >= 5
        and len(fast_pairs) >= 5
        and speed_separation >= 0.06
        and np.isfinite(condition)
        and condition < 250.0
    )
    physically_plausible = bool(coulomb >= -0.02 and viscous >= -0.02)
    residual_limit = max(0.12, 0.25 * float(np.std(y)) + 0.02)
    accepted = bool(friction_identifiable and physically_plausible and validation_rms <= residual_limit)

    return FrictionFitResult(
        coulomb_friction_nm=coulomb,
        viscous_friction_nm_per_rad_s=viscous,
        training_rms_residual_nm=training_rms,
        validation_rms_residual_nm=validation_rms,
        validation_max_residual_nm=validation_max,
        condition_number=condition,
        pair_count=len(pairs),
        slow_pair_count=len(slow_pairs),
        fast_pair_count=len(fast_pairs),
        slow_speed_rad_s=slow_speed,
        fast_speed_rad_s=fast_speed,
        friction_identifiable=friction_identifiable,
        factory_gravity_retained=True,
        accepted=accepted,
    )
