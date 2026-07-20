from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from orbbec_femto_provider.device_calibration import validate_calibration_document


@dataclass(frozen=True)
class AffineCalibrationFit:
    scale: tuple[float, float, float]
    offset: tuple[float, float, float]
    axis_rms_residual_m_s2: tuple[float, float, float]
    sample_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "scale": list(self.scale),
            "offset": list(self.offset),
            "axis_rms_residual_m_s2": list(self.axis_rms_residual_m_s2),
            "sample_count": self.sample_count,
        }


def fit_axis_affine_calibration(
    measured_m_s2: np.ndarray,
    expected_m_s2: np.ndarray,
) -> AffineCalibrationFit:
    """Fit expected = scale * measured + offset independently per axis."""
    measured = np.asarray(measured_m_s2, dtype=np.float64)
    expected = np.asarray(expected_m_s2, dtype=np.float64)
    if measured.ndim != 2 or measured.shape[1] != 3:
        raise ValueError("measured_m_s2 must have shape (N, 3)")
    if expected.shape != measured.shape:
        raise ValueError("expected_m_s2 must have the same shape as measured_m_s2")
    if measured.shape[0] < 6:
        raise ValueError("at least six calibration observations are required")
    if not np.isfinite(measured).all() or not np.isfinite(expected).all():
        raise ValueError("calibration observations must be finite")

    scales: list[float] = []
    offsets: list[float] = []
    residuals: list[float] = []
    for axis in range(3):
        design = np.column_stack((measured[:, axis], np.ones(measured.shape[0])))
        solution, _, rank, _ = np.linalg.lstsq(design, expected[:, axis], rcond=None)
        if rank < 2:
            raise ValueError(f"axis {axis} does not contain enough excitation for scale and offset")
        scale, offset = (float(item) for item in solution)
        prediction = design @ solution
        rms = float(np.sqrt(np.mean(np.square(prediction - expected[:, axis]))))
        scales.append(scale)
        offsets.append(offset)
        residuals.append(rms)

    return AffineCalibrationFit(
        scale=tuple(scales),
        offset=tuple(offsets),
        axis_rms_residual_m_s2=tuple(residuals),
        sample_count=int(measured.shape[0]),
    )


def write_custom_accelerometer_calibration(
    calibration_path: Path,
    *,
    expected_device_id: str,
    fit: AffineCalibrationFit,
    method: str,
    operator: str | None = None,
    temperature_c: float | None = None,
) -> dict[str, Any]:
    """Replace an identity/device calibration file with a fitted revision atomically."""
    document = json.loads(calibration_path.read_text(encoding="utf-8"))
    validate_calibration_document(
        calibration_path,
        document,
        expected_device_id=expected_device_id,
    )
    now = datetime.now(timezone.utc).isoformat()
    document["status"] = "CUSTOM_CALIBRATED"
    correction = document.setdefault("correction", {})
    correction.update(
        {
            "equation": "corrected_equals_scale_times_input_plus_offset",
            "scale": list(fit.scale),
            "offset": list(fit.offset),
            "input_units": "m/s^2",
            "output_units": "m/s^2",
        }
    )
    provenance = document.setdefault("provenance", {})
    provenance.update(
        {
            "method": method,
            "calibrated_at": now,
            "operator": operator,
            "sample_count": fit.sample_count,
        }
    )
    document["quality"] = {
        "axis_rms_residual_m_s2": list(fit.axis_rms_residual_m_s2),
        "temperature_c": temperature_c,
        "validated": True,
    }
    _atomic_json_write(calibration_path, document)
    validated = validate_calibration_document(
        calibration_path,
        document,
        expected_device_id=expected_device_id,
    )
    return {
        "status": validated.status,
        "revision": validated.revision,
        "path": str(validated.path),
        "fit": fit.to_dict(),
    }


def _atomic_json_write(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(document, indent=2, sort_keys=True) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
