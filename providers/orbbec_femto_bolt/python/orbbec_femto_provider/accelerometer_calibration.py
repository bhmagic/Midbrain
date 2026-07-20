from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

import numpy as np

STANDARD_GRAVITY_M_S2 = 9.80665
POSE_ORDER = ("x+", "x-", "y+", "y-", "z+", "z-")
POSE_AXIS = {
    "x+": (0, 1.0),
    "x-": (0, -1.0),
    "y+": (1, 1.0),
    "y-": (1, -1.0),
    "z+": (2, 1.0),
    "z-": (2, -1.0),
}


@dataclass(frozen=True)
class CaptureSummary:
    pose: str
    sample_count: int
    mean_m_s2: tuple[float, float, float]
    std_m_s2: tuple[float, float, float]
    mean_temperature_c: float | None
    duration_s: float
    first_frame_number: int
    last_frame_number: int

    @property
    def magnitude_m_s2(self) -> float:
        return math.sqrt(sum(value * value for value in self.mean_m_s2))

    def to_dict(self) -> dict[str, Any]:
        return {
            "pose": self.pose,
            "sample_count": self.sample_count,
            "mean_m_s2": list(self.mean_m_s2),
            "std_m_s2": list(self.std_m_s2),
            "mean_temperature_c": self.mean_temperature_c,
            "duration_s": self.duration_s,
            "first_frame_number": self.first_frame_number,
            "last_frame_number": self.last_frame_number,
            "magnitude_m_s2": self.magnitude_m_s2,
        }


@dataclass(frozen=True)
class CalibrationSolution:
    scale: tuple[float, float, float]
    offset: tuple[float, float, float]
    linear_scale: tuple[float, float, float]
    linear_offset: tuple[float, float, float]
    corrected_magnitudes_m_s2: tuple[float, ...]
    residuals_m_s2: tuple[float, ...]
    rms_residual_m_s2: float
    max_abs_residual_m_s2: float
    design_condition_number: float
    nonlinear_iterations: int
    converged: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "scale": list(self.scale),
            "offset": list(self.offset),
            "linearized_initial_scale": list(self.linear_scale),
            "linearized_initial_offset": list(self.linear_offset),
            "corrected_magnitudes_m_s2": list(self.corrected_magnitudes_m_s2),
            "residuals_m_s2": list(self.residuals_m_s2),
            "rms_residual_m_s2": self.rms_residual_m_s2,
            "max_abs_residual_m_s2": self.max_abs_residual_m_s2,
            "design_condition_number": self.design_condition_number,
            "nonlinear_iterations": self.nonlinear_iterations,
            "converged": self.converged,
        }


def summarize_capture(
    pose: str,
    vectors_m_s2: Sequence[Sequence[float]],
    temperatures_c: Sequence[float],
    *,
    duration_s: float,
    first_frame_number: int,
    last_frame_number: int,
) -> CaptureSummary:
    _validate_pose(pose)
    values = np.asarray(vectors_m_s2, dtype=np.float64)
    if values.ndim != 2 or values.shape[1] != 3 or values.shape[0] < 2:
        raise ValueError("at least two three-axis accelerometer samples are required")
    if not np.isfinite(values).all():
        raise ValueError("accelerometer samples must be finite")
    temperatures = np.asarray(temperatures_c, dtype=np.float64)
    finite_temperatures = temperatures[np.isfinite(temperatures)]
    mean_temperature = (
        float(np.mean(finite_temperatures)) if finite_temperatures.size else None
    )
    mean = np.mean(values, axis=0)
    std = np.std(values, axis=0, ddof=1)
    return CaptureSummary(
        pose=pose,
        sample_count=int(values.shape[0]),
        mean_m_s2=tuple(float(value) for value in mean),
        std_m_s2=tuple(float(value) for value in std),
        mean_temperature_c=mean_temperature,
        duration_s=float(duration_s),
        first_frame_number=int(first_frame_number),
        last_frame_number=int(last_frame_number),
    )


def pose_quality(
    capture: CaptureSummary,
    *,
    gravity_m_s2: float = STANDARD_GRAVITY_M_S2,
) -> dict[str, Any]:
    axis_index, expected_sign = POSE_AXIS[capture.pose]
    mean = np.asarray(capture.mean_m_s2, dtype=np.float64)
    std = np.asarray(capture.std_m_s2, dtype=np.float64)
    target_component = float(mean[axis_index] * expected_sign)
    cross_components = np.delete(np.abs(mean), axis_index)
    magnitude = float(np.linalg.norm(mean))
    rms_noise = float(np.linalg.norm(std))

    warnings: list[str] = []
    if target_component < 0.70 * gravity_m_s2:
        warnings.append("target axis is not strongly aligned with gravity")
    if float(np.max(cross_components)) > 0.55 * gravity_m_s2:
        warnings.append("a non-target axis is too large")
    if not 0.70 * gravity_m_s2 <= magnitude <= 1.30 * gravity_m_s2:
        warnings.append("measured gravity magnitude is outside the expected range")
    if rms_noise > 0.12:
        warnings.append("camera moved during the capture")
    if capture.sample_count < 20:
        warnings.append("too few samples were captured")

    return {
        "accepted": not warnings,
        "warnings": warnings,
        "target_component_m_s2": target_component,
        "largest_cross_axis_m_s2": float(np.max(cross_components)),
        "magnitude_m_s2": magnitude,
        "rms_noise_m_s2": rms_noise,
    }


def solve_six_position_calibration(
    captures: Mapping[str, CaptureSummary],
    *,
    gravity_m_s2: float = STANDARD_GRAVITY_M_S2,
    max_iterations: int = 20,
) -> CalibrationSolution:
    missing = [pose for pose in POSE_ORDER if pose not in captures]
    if missing:
        raise ValueError(f"missing calibration poses: {', '.join(missing)}")
    if gravity_m_s2 <= 0.0 or not math.isfinite(gravity_m_s2):
        raise ValueError("gravity must be a positive finite value")

    raw = np.asarray(
        [captures[pose].mean_m_s2 for pose in POSE_ORDER],
        dtype=np.float64,
    )
    if raw.shape != (6, 3) or not np.isfinite(raw).all():
        raise ValueError("six finite three-axis capture means are required")

    # First-order model around scale=1 and offset=0:
    # sum(x_i^2 + 2*delta_i*x_i^2 + 2*b_i*x_i) = g^2.
    design = np.column_stack((2.0 * raw * raw, 2.0 * raw))
    target = gravity_m_s2 * gravity_m_s2 - np.sum(raw * raw, axis=1)
    condition = float(np.linalg.cond(design))
    if not math.isfinite(condition) or condition > 1.0e8:
        raise ValueError(
            "six-position system is ill-conditioned; retake the poses with stronger "
            "+/- axis alignment"
        )

    linear_parameters, _, rank, _ = np.linalg.lstsq(design, target, rcond=None)
    if rank < 6:
        raise ValueError(
            "six-position system does not contain six independent equations; retake "
            "the poses with stronger +/- axis alignment"
        )
    linear_scale = 1.0 + linear_parameters[:3]
    linear_offset = linear_parameters[3:]

    parameters = np.concatenate((linear_scale, linear_offset))
    converged = False
    iterations = 0
    for iterations in range(1, max_iterations + 1):
        scale = parameters[:3]
        offset = parameters[3:]
        corrected = raw * scale + offset
        residual_squared = np.sum(corrected * corrected, axis=1) - gravity_m_s2**2
        jacobian = np.column_stack((2.0 * raw * corrected, 2.0 * corrected))

        # Mild Levenberg damping protects the exactly-determined six-equation
        # system from a noisy or nearly singular update.
        damping = 1.0e-8 * max(1.0, float(np.linalg.norm(jacobian, ord=2)) ** 2)
        normal = jacobian.T @ jacobian + damping * np.eye(6)
        right = -(jacobian.T @ residual_squared)
        try:
            step = np.linalg.solve(normal, right)
        except np.linalg.LinAlgError as error:
            raise ValueError("nonlinear calibration refinement is singular") from error

        candidate = parameters + step
        if np.any(candidate[:3] <= 0.2) or np.any(candidate[:3] >= 5.0):
            raise ValueError("calculated accelerometer scale is physically unreasonable")
        if np.any(np.abs(candidate[3:]) > 5.0):
            raise ValueError("calculated accelerometer offset is physically unreasonable")
        parameters = candidate
        if float(np.linalg.norm(step)) < 1.0e-12:
            converged = True
            break

    scale = parameters[:3]
    offset = parameters[3:]
    corrected = raw * scale + offset
    corrected_magnitudes = np.linalg.norm(corrected, axis=1)
    residuals = corrected_magnitudes - gravity_m_s2
    rms = float(math.sqrt(float(np.mean(residuals * residuals))))
    maximum = float(np.max(np.abs(residuals)))

    if not np.isfinite(parameters).all() or not np.isfinite(residuals).all():
        raise ValueError("calibration produced non-finite coefficients")
    if rms > 0.20 or maximum > 0.35:
        raise ValueError(
            "calibration residual is too large; keep the camera still and retake all poses"
        )

    return CalibrationSolution(
        scale=tuple(float(value) for value in scale),
        offset=tuple(float(value) for value in offset),
        linear_scale=tuple(float(value) for value in linear_scale),
        linear_offset=tuple(float(value) for value in linear_offset),
        corrected_magnitudes_m_s2=tuple(float(value) for value in corrected_magnitudes),
        residuals_m_s2=tuple(float(value) for value in residuals),
        rms_residual_m_s2=rms,
        max_abs_residual_m_s2=maximum,
        design_condition_number=condition,
        nonlinear_iterations=iterations,
        converged=converged,
    )


def build_custom_calibration_document(
    existing_document: Mapping[str, Any],
    captures: Mapping[str, CaptureSummary],
    solution: CalibrationSolution,
    *,
    gravity_m_s2: float = STANDARD_GRAVITY_M_S2,
) -> dict[str, Any]:
    document = _deep_copy_json(existing_document)
    now = datetime.now(timezone.utc).isoformat()
    document["status"] = "CUSTOM_CALIBRATED"
    correction = dict(document.get("correction") or {})
    correction.update(
        {
            "equation": "corrected_equals_scale_times_input_plus_offset",
            "scale": list(solution.scale),
            "offset": list(solution.offset),
            "input_units": "m/s^2",
            "output_units": "m/s^2",
            "input_stage": "sdk_si_after_factory_processing",
        }
    )
    document["correction"] = correction
    document["provenance"] = {
        "method": "six_position_static_gravity_norm_fit",
        "created_at": now,
        "gravity_m_s2": gravity_m_s2,
        "linearization": (
            "scale=1+delta; retained first-order terms "
            "2*delta*x^2 and 2*b*x"
        ),
        "refinement": "damped_gauss_newton_on_original_norm_equations",
        "capture_order": list(POSE_ORDER),
    }
    document["quality"] = {
        "validated": True,
        "rms_magnitude_residual_m_s2": solution.rms_residual_m_s2,
        "max_abs_magnitude_residual_m_s2": solution.max_abs_residual_m_s2,
        "design_condition_number": solution.design_condition_number,
        "nonlinear_iterations": solution.nonlinear_iterations,
        "nonlinear_converged": solution.converged,
        "capture_temperature_c": _mean_capture_temperature(captures),
    }
    document["captures"] = {
        pose: captures[pose].to_dict() for pose in POSE_ORDER
    }
    document["fit"] = solution.to_dict()
    return document


def _mean_capture_temperature(
    captures: Mapping[str, CaptureSummary],
) -> float | None:
    values = [
        capture.mean_temperature_c
        for capture in captures.values()
        if capture.mean_temperature_c is not None
    ]
    return float(sum(values) / len(values)) if values else None


def _deep_copy_json(value: Mapping[str, Any]) -> dict[str, Any]:
    import json

    return json.loads(json.dumps(value))


def _validate_pose(pose: str) -> None:
    if pose not in POSE_AXIS:
        raise ValueError(f"unsupported calibration pose: {pose}")
