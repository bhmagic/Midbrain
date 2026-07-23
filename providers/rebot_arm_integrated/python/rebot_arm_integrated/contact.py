from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import numpy as np


def _six(values: Iterable[float], name: str) -> np.ndarray:
    result = np.asarray(list(values), dtype=float)
    if result.shape != (6,) or not np.all(np.isfinite(result)):
        raise ValueError(f"{name} must contain six finite values")
    return result


@dataclass(frozen=True)
class TorqueBaseline:
    positions_rad: np.ndarray
    torques_nm: np.ndarray
    gravity_nm: np.ndarray
    torque_mad_nm: np.ndarray
    sample_count: int

    @classmethod
    def from_samples(
        cls,
        samples: list[dict],
        *,
        maximum_velocity_rad_s: float | Iterable[float],
        maximum_mad_nm: Iterable[float],
    ) -> "TorqueBaseline":
        if len(samples) < 5:
            raise ValueError("at least five steady samples are required for a torque baseline")
        positions = np.asarray([sample["positions_rad"][:6] for sample in samples], dtype=float)
        velocities = np.asarray([sample["velocities_rad_s"][:6] for sample in samples], dtype=float)
        torques = np.asarray([sample["torques_nm"][:6] for sample in samples], dtype=float)
        gravity = np.asarray(
            [sample["gravity_compensation"]["total_nm"][:6] for sample in samples],
            dtype=float,
        )
        for name, values in (("positions", positions), ("velocities", velocities), ("torques", torques), ("gravity", gravity)):
            if values.shape != (len(samples), 6) or not np.all(np.isfinite(values)):
                raise ValueError(f"baseline {name} samples must be finite six-joint vectors")
        raw_speed_limit = np.asarray(maximum_velocity_rad_s, dtype=float)
        speed_limit = (
            np.full(6, float(raw_speed_limit), dtype=float)
            if raw_speed_limit.ndim == 0
            else _six(raw_speed_limit, "maximum_velocity_rad_s")
        )
        if np.any(speed_limit <= 0.0) or not np.all(np.isfinite(speed_limit)):
            raise ValueError("maximum_velocity_rad_s must contain positive finite values")
        if np.any(np.abs(velocities) > speed_limit):
            raise ValueError("arm velocity is too high to capture a steady torque baseline")
        median_torque = np.median(torques, axis=0)
        mad = np.median(np.abs(torques - median_torque), axis=0)
        mad_limit = _six(maximum_mad_nm, "maximum_mad_nm")
        if np.any(mad_limit <= 0.0):
            raise ValueError("maximum_mad_nm values must be positive")
        if np.any(mad > mad_limit):
            joints = [str(index + 1) for index in np.flatnonzero(mad > mad_limit)]
            raise ValueError(f"torque baseline is not steady for joints {', '.join(joints)}")
        return cls(
            np.median(positions, axis=0),
            median_torque,
            np.median(gravity, axis=0),
            mad,
            len(samples),
        )

    def expected_torque(self, current_gravity_nm: Iterable[float]) -> np.ndarray:
        current_gravity = _six(current_gravity_nm, "current_gravity_nm")
        return self.torques_nm + current_gravity - self.gravity_nm

    def residual(
        self,
        measured_torque_nm: Iterable[float],
        current_gravity_nm: Iterable[float],
    ) -> np.ndarray:
        measured = _six(measured_torque_nm, "measured_torque_nm")
        return measured - self.expected_torque(current_gravity_nm)

    def snapshot(self) -> dict:
        return {
            "positions_rad": self.positions_rad.tolist(),
            "torques_nm": self.torques_nm.tolist(),
            "gravity_nm": self.gravity_nm.tolist(),
            "torque_mad_nm": self.torque_mad_nm.tolist(),
            "sample_count": self.sample_count,
        }


def force_position_ratios(
    expected_torque_nm: Iterable[float],
    allowed_external_torque_nm: Iterable[float],
    configured_tmax_nm: Iterable[float],
    provider_ratio_caps: Iterable[float],
    *,
    margin_nm: Iterable[float],
    saturate_at_caps: bool = False,
) -> np.ndarray:
    expected = np.abs(_six(expected_torque_nm, "expected_torque_nm"))
    external = _six(allowed_external_torque_nm, "allowed_external_torque_nm")
    tmax = _six(configured_tmax_nm, "configured_tmax_nm")
    caps = _six(provider_ratio_caps, "provider_ratio_caps")
    margin = _six(margin_nm, "margin_nm")
    if np.any(external <= 0.0):
        raise ValueError("allowed external torque must be explicitly positive for every joint")
    if np.any(tmax <= 0.0) or np.any(caps <= 0.0) or np.any(caps > 1.0):
        raise ValueError("motor torque and provider ratio limits are invalid")
    if np.any(margin < 0.0):
        raise ValueError("torque margins must be non-negative")
    required = (expected + external + margin) / tmax
    if np.any(required > caps + 1e-12):
        if saturate_at_caps:
            return np.minimum(required, caps)
        joints = [str(index + 1) for index in np.flatnonzero(required > caps + 1e-12)]
        raise ValueError(
            "requested baseline plus external torque budget exceeds configured physical POS_TOR caps for joints "
            + ", ".join(joints)
        )
    return required


def cartesian_wrench_to_joint_budget(
    geometric_jacobian: np.ndarray,
    controlled_frame_rotation: np.ndarray,
    force_budget_n: Iterable[float],
    torque_budget_nm: Iterable[float],
    *,
    minimum_joint_budget_nm: Iterable[float],
) -> np.ndarray:
    """Map a controlled-frame wrench box to conservative per-joint torque limits."""
    jacobian = np.asarray(geometric_jacobian, dtype=float)
    rotation = np.asarray(controlled_frame_rotation, dtype=float)
    force = np.asarray(list(force_budget_n), dtype=float)
    torque = np.asarray(list(torque_budget_nm), dtype=float)
    minimum = _six(minimum_joint_budget_nm, "minimum_joint_budget_nm")
    if jacobian.shape != (6, 6) or not np.all(np.isfinite(jacobian)):
        raise ValueError("geometric_jacobian must be a finite 6x6 matrix")
    if rotation.shape != (3, 3) or not np.all(np.isfinite(rotation)):
        raise ValueError("controlled_frame_rotation must be a finite 3x3 matrix")
    if force.shape != (3,) or torque.shape != (3,):
        raise ValueError("Cartesian force and torque budgets must each contain three values")
    if (
        not np.all(np.isfinite(force))
        or not np.all(np.isfinite(torque))
        or np.any(force < 0.0)
        or np.any(torque < 0.0)
        or np.any(minimum <= 0.0)
    ):
        raise ValueError("Cartesian wrench budgets must be finite and non-negative")
    if not np.any(force > 0.0) and not np.any(torque > 0.0):
        raise ValueError("at least one Cartesian wrench budget component must be positive")
    wrench_rotation = np.zeros((6, 6), dtype=float)
    wrench_rotation[:3, :3] = rotation
    wrench_rotation[3:, 3:] = rotation
    joint_from_controlled_wrench = jacobian.T @ wrench_rotation
    mapped = np.abs(joint_from_controlled_wrench) @ np.concatenate([force, torque])
    return np.maximum(mapped, minimum)


def isotropic_wrench_to_joint_budget(
    geometric_jacobian: np.ndarray,
    controlled_frame_rotation: np.ndarray,
    force_magnitude_budget_n: float,
    torque_magnitude_budget_nm: float,
    *,
    minimum_joint_budget_nm: Iterable[float],
) -> np.ndarray:
    """Map isotropic force and torque magnitude balls to per-joint limits."""
    jacobian = np.asarray(geometric_jacobian, dtype=float)
    rotation = np.asarray(controlled_frame_rotation, dtype=float)
    minimum = _six(minimum_joint_budget_nm, "minimum_joint_budget_nm")
    force = float(force_magnitude_budget_n)
    torque = float(torque_magnitude_budget_nm)
    if jacobian.shape != (6, 6) or not np.all(np.isfinite(jacobian)):
        raise ValueError("geometric_jacobian must be a finite 6x6 matrix")
    if rotation.shape != (3, 3) or not np.all(np.isfinite(rotation)):
        raise ValueError("controlled_frame_rotation must be a finite 3x3 matrix")
    if (
        not np.isfinite(force)
        or not np.isfinite(torque)
        or force < 0.0
        or torque < 0.0
        or np.any(minimum <= 0.0)
    ):
        raise ValueError("isotropic wrench budgets must be finite and non-negative")
    if force == 0.0 and torque == 0.0:
        raise ValueError("at least one isotropic wrench budget must be positive")
    wrench_rotation = np.zeros((6, 6), dtype=float)
    wrench_rotation[:3, :3] = rotation
    wrench_rotation[3:, 3:] = rotation
    joint_from_controlled_wrench = jacobian.T @ wrench_rotation
    mapped = (
        force * np.linalg.norm(joint_from_controlled_wrench[:, :3], axis=1)
        + torque * np.linalg.norm(joint_from_controlled_wrench[:, 3:], axis=1)
    )
    return np.maximum(mapped, minimum)


def torque_limit_violations(residual_nm: Iterable[float], limits_nm: Iterable[float]) -> list[int]:
    residual = _six(residual_nm, "residual_nm")
    limits = _six(limits_nm, "limits_nm")
    if np.any(limits <= 0.0):
        raise ValueError("torque residual limits must be explicitly positive")
    return [int(index) for index in np.flatnonzero(np.abs(residual) > limits)]
