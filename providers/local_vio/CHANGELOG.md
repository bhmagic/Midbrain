# Changelog

## 0.4.1 - 2026-08-07

- Matched RGB and registered-depth inputs from retained provider-local ring
  slots instead of independently reading two latest slots across the D2C
  processing boundary.
- Used sensor capture timestamps to qualify visual synchronization while
  retaining system time for camera/IMU estimator ordering.
- Added frame, capture-delta, and system-delta evidence when no retained pair
  satisfies the declared synchronization threshold.

## 0.4.0 - 2026-07-30

- Added bounded fixed-rig stationary attestation for initialization without
  acquiring the global motion-inhibit lease, resetting the VIO epoch, or
  revoking an arm-controller lease.

## 0.3.0 - 2026-07-30

- Migrated new epochs to
  `MIDBRAIN_X_FORWARD_Y_LEFT_Z_UP_V2`, retained explicit optical-camera axes,
  and marked older Y-up epochs as historical rather than reinterpreting them.

## 0.2.3 - 2026-07-29

- Reacquired current Provider-local shared-memory references for every visual
  iteration instead of treating cached Fabric BufferRefs as image storage.

## 0.2.2

- Made stationary initialization independent of an assumed camera/IMU sample
  rate by selecting a fixed-count window with a bounded age.

## 0.2.1

- Moved startup stationarity selection into the common IMU time domain and
  exposed history, skew, and blocker diagnostics.

## 0.2.0

- Replaced the visual-first loop with a 15-state inertial error-state filter,
  high-rate non-committing propagation, gated RGB-D corrections, and optional
  synchronized IR/depth fallback.
- Added covariance, bias, correction, staleness, and propagation diagnostics
  while preserving epoch and motion-inhibit boundaries.

## 0.1.6

- Added full 3D visual/gyro disagreement checks and bounded gyro-only degraded
  propagation when visual rotation is unavailable.
