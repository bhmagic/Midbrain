# Device Calibration Contract

Status: v0.3 working draft.

## Identity and persistence

Device calibration is stored outside replaceable packages under persistent workspace configuration. The physical key is namespaced manufacturer, model, and manufacturer serial number. USB port, enumeration index, host device path, process instance, and host identity are not physical-device keys.

Recommended path:

`config/calibration/devices/<manufacturer>/<model>/<serial>/imu-accelerometer.json`

## First-seen policy

When a valid serial is first observed, the owning hardware Provider atomically creates an `UNCALIBRATED` record with:

- `corrected = scale * input + offset`
- scale `[1.0, 1.0, 1.0]`
- offset `[0.0, 0.0, 0.0]`

This identity correction is an operational fallback, not evidence of measured calibration. The status and revision must be published with the canonical IMU stream.

## Ownership

The Resource Provider that owns the physical sensor loads, validates, and applies the persistent calibration before publishing canonical SI-unit observations. Consumers such as VIO use the corrected stream. Optional raw or pre-custom-correction values retain provenance for diagnostics and later fitting.

## Runtime bias separation

Persistent device calibration and runtime estimator bias are separate. VIO may estimate time-varying residual accelerometer and gyroscope bias within a session, publish those estimates, and reset them with the VIO epoch. It must not automatically rewrite the persistent device calibration.

## Calibration Skill

A finite accelerometer calibration Skill collects or accepts stationary orientation samples, fits each axis independently, validates residuals, writes a new `CUSTOM_CALIBRATED` revision atomically, and requests the hardware Provider to reload it. Mismatched device identity, invalid units, insufficient axis excitation, or malformed coefficients cause failure without replacing the last valid revision.
