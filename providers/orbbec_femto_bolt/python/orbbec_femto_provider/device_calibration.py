from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CALIBRATION_SCHEMA = "physical_agent.imu_accelerometer_calibration"
CALIBRATION_SCHEMA_VERSION = 1


class DeviceIdentityError(ValueError):
    pass


@dataclass(frozen=True)
class AccelerometerCalibration:
    path: Path
    canonical_device_id: str
    status: str
    scale: tuple[float, float, float]
    offset: tuple[float, float, float]
    revision: str
    document: dict[str, Any]

    def apply(self, x: float, y: float, z: float) -> tuple[float, float, float]:
        values = (x, y, z)
        return tuple(
            self.scale[index] * values[index] + self.offset[index]
            for index in range(3)
        )


def canonical_device_id(manufacturer: str, model: str, serial_number: str) -> str:
    serial = validate_serial(serial_number)
    manufacturer_slug = _slug(manufacturer)
    model_slug = _slug(model)
    return f"{manufacturer_slug}:{model_slug}:{serial}"


def validate_serial(serial_number: str) -> str:
    serial = str(serial_number or "").strip()
    if not serial:
        raise DeviceIdentityError("device serial number is empty")
    compact = re.sub(r"[^A-Za-z0-9._-]", "", serial)
    if compact != serial:
        raise DeviceIdentityError("device serial number contains unsupported characters")
    lowered = serial.lower()
    if set(lowered) <= {"0", "-", "_", "."}:
        raise DeviceIdentityError("device serial number is an all-zero placeholder")
    if lowered in {"unknown", "none", "null", "default", "placeholder"}:
        raise DeviceIdentityError("device serial number is a placeholder")
    return serial


def accelerometer_calibration_path(
    calibration_root: Path,
    *,
    manufacturer: str,
    model: str,
    serial_number: str,
) -> Path:
    serial = validate_serial(serial_number)
    return (
        calibration_root
        / _slug(manufacturer)
        / _slug(model)
        / serial
        / "imu-accelerometer.json"
    )


def write_accelerometer_calibration_document(
    path: Path,
    document: dict[str, Any],
    *,
    expected_device_id: str,
    backup_existing: bool = True,
) -> AccelerometerCalibration:
    validate_calibration_document(
        path,
        document,
        expected_device_id=expected_device_id,
    )
    if backup_existing and path.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = path.with_name(f"{path.stem}.before-{stamp}{path.suffix}")
        shutil.copy2(path, backup)
    _atomic_json_write(path, document)
    return validate_calibration_document(
        path,
        document,
        expected_device_id=expected_device_id,
    )


def load_or_create_accelerometer_calibration(
    calibration_root: Path,
    *,
    manufacturer: str,
    model: str,
    serial_number: str,
    firmware_version: str | None,
) -> AccelerometerCalibration:
    serial = validate_serial(serial_number)
    device_id = canonical_device_id(manufacturer, model, serial)
    path = accelerometer_calibration_path(
        calibration_root,
        manufacturer=manufacturer,
        model=model,
        serial_number=serial,
    )
    if not path.exists():
        document = _identity_document(
            manufacturer=manufacturer,
            model=model,
            serial_number=serial,
            canonical_id=device_id,
            firmware_version=firmware_version,
        )
        _atomic_json_write(path, document)
    document = json.loads(path.read_text(encoding="utf-8"))
    return validate_calibration_document(path, document, expected_device_id=device_id)


def validate_calibration_document(
    path: Path,
    document: dict[str, Any],
    *,
    expected_device_id: str,
) -> AccelerometerCalibration:
    if document.get("schema") != CALIBRATION_SCHEMA:
        raise ValueError(f"unsupported accelerometer calibration schema in {path}")
    if int(document.get("schema_version", 0)) != CALIBRATION_SCHEMA_VERSION:
        raise ValueError(f"unsupported accelerometer calibration schema version in {path}")
    device = document.get("device") or {}
    if device.get("canonical_device_id") != expected_device_id:
        raise ValueError(f"accelerometer calibration belongs to another device: {path}")
    correction = document.get("correction") or {}
    if correction.get("equation") != "corrected_equals_scale_times_input_plus_offset":
        raise ValueError(f"unsupported accelerometer correction equation in {path}")
    scale = _vector3(correction.get("scale"), field="scale")
    offset = _vector3(correction.get("offset"), field="offset")
    status = str(document.get("status") or "INVALID").upper()
    if status not in {"UNCALIBRATED", "CUSTOM_CALIBRATED", "FACTORY_ONLY"}:
        raise ValueError(f"unsupported accelerometer calibration status in {path}: {status}")
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    revision = hashlib.sha256(canonical).hexdigest()[:16]
    return AccelerometerCalibration(
        path=path,
        canonical_device_id=expected_device_id,
        status=status,
        scale=scale,
        offset=offset,
        revision=revision,
        document=document,
    )


def _identity_document(
    *,
    manufacturer: str,
    model: str,
    serial_number: str,
    canonical_id: str,
    firmware_version: str | None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "schema": CALIBRATION_SCHEMA,
        "schema_version": CALIBRATION_SCHEMA_VERSION,
        "status": "UNCALIBRATED",
        "device": {
            "manufacturer": manufacturer,
            "model": model,
            "serial_number": serial_number,
            "canonical_device_id": canonical_id,
        },
        "sensor": {
            "kind": "accelerometer",
            "coordinate_frame": "femto_bolt_imu_frame",
            "axis_order": ["x", "y", "z"],
        },
        "correction": {
            "equation": "corrected_equals_scale_times_input_plus_offset",
            "scale": [1.0, 1.0, 1.0],
            "offset": [0.0, 0.0, 0.0],
            "input_units": "m/s^2",
            "output_units": "m/s^2",
            "input_stage": "sdk_si_after_factory_processing",
        },
        "provenance": {
            "method": "identity_first_seen_default",
            "created_at": now,
            "firmware_version_at_creation": firmware_version,
            "note": "Operational fallback only; replace through the accelerometer calibration GUI.",
        },
        "quality": {
            "fit_residual": None,
            "temperature_c": None,
            "validated": False,
        },
    }


def _atomic_json_write(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(document, indent=2, sort_keys=True) + "\n"
    fd, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
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


def _slug(value: str) -> str:
    result = re.sub(r"[^a-z0-9]+", "-", str(value).strip().lower()).strip("-")
    if not result:
        raise DeviceIdentityError("device manufacturer/model cannot be empty")
    return result


def _vector3(value: Any, *, field: str) -> tuple[float, float, float]:
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError(f"accelerometer calibration {field} must have three values")
    result = tuple(float(item) for item in value)
    if not all(item == item and abs(item) != float("inf") for item in result):
        raise ValueError(f"accelerometer calibration {field} must be finite")
    return result
