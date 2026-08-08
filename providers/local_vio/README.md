# Local VIO Resource Provider

The default backend is an inertial-first RGB-D visual-inertial estimator. Every ordered accelerometer and gyroscope sample propagates a 15-state error-state filter containing orientation, position, velocity, gyroscope bias, accelerometer bias, and covariance. Camera observations are correction measurements rather than the primary motion clock.

## Estimator architecture

- IMU propagation runs at retained IMU sample rate.
- A non-committing fast propagation path publishes pose predictions between camera frames.
- RGB plus aligned depth is the preferred metric visual correction source.
- Infrared plus native depth is an optional synchronized low-light fallback.
- Visual corrections use innovation and Mahalanobis gates before changing the inertial state.
- RGB-D visual tracking retains the raw ORB/PnP baseline and optional circular local-contrast-normalized candidate.
- Depth resolves metric translation directly and avoids monocular scale ambiguity.
- Published state includes estimated IMU biases, pose covariance, visual correction size, visual staleness, and IMU propagation counts.

This Python implementation is a reference error-state filter with frame-to-frame RGB-D/IR pose updates. It is not a complete feature-level MSCKF or fixed-lag nonlinear optimizer. The Provider contract remains compatible with a future native OpenVINS- or Basalt-class backend.

## Gravity policy

The established quiet-IMU gravity behavior is retained:

- Gravity corrects roll and pitch only.
- Yaw and translation are unchanged by gravity leveling.
- The configured quiet threshold is `0.012 rad/s`.
- Startup estimates gyroscope zero-rate bias and a robust residual-noise ceiling.
- The effective threshold is `max(0.012, 1.5 * robust_noise_ceiling)`, clamped to `0.008..0.03 rad/s`.
- Tracking correction is small and bounded; degraded recovery is stronger but remains bounded.
- Gravity status is independently reported as `OFF`, `READY`, or `ACTIVE`.

The world basis is
`MIDBRAIN_X_FORWARD_Y_LEFT_Z_UP_V2`: positive X is initial leveled
camera/body forward, positive Y is left, positive Z is opposite gravity, and
the world gravity vector is `[0, 0, -9.80665]` m/s2. Raw optical sensing
remains X image-right, Y image-down, Z optical-forward. The Provider publishes
a derived gravity-leveled camera frame without changing the optical axes.
Calibration and RGB-D inputs without the explicit native optical convention
identifier are rejected rather than interpreted as anonymous XYZ data.

This change does not modify accelerometer bias, scale, hardware axis metadata,
device identity, calibration revision, or camera/IMU extrinsics.

## Visual correction sources

### RGB-D

RGB features and RGB-aligned depth are the primary correction source. The original raw grayscale ORB/PnP path always runs first. Circular local contrast normalization is only an additional candidate in dim or low-contrast frames and must materially outperform the raw result before selection.

The Provider selects RGB and registered depth as a retained synchronized pair,
not as two independent latest slots. Sensor capture timestamps qualify the
pair; system timestamps remain the common ordering domain used by the visual
and IMU estimator inputs. D2C processing latency is therefore never treated as
camera motion or as capture-time skew.

### Infrared and native depth

IR is used only when:

- IR support is enabled.
- IR, native depth, and the current RGB frame are within the configured timestamp tolerance.
- IR intrinsics and IR-to-color extrinsics are available.
- RGB correction is unavailable or materially weaker.

If native depth and IR resolutions differ, depth is resized with nearest-neighbor sampling before IR feature geometry is evaluated. IR remains a correction fallback, not a separate pose authority.

## Published streams

- `localization.vio.status`
- `localization.body.pose`
- `localization.vio.bias`
- `transform.local_vio.body`
- `transform.local_vio.camera_level`

## Important limitations

- Camera/IMU time offset is not yet estimated online.
- Visual updates are pose-level RGB-D/IR corrections rather than feature-level MSCKF updates.
- Long visual outages still allow inertial drift, especially in position.
- Hardware accuracy must be measured against a recorded trajectory or external reference.

## Stationary initialization

The initializer selects a fixed-count accelerometer and gyroscope window in a
common IMU timestamp domain. It does not assume that the window fits inside a
fixed wall-clock interval. Provider status reports the selected sample counts,
inferred rates, timestamp skew, and any initialization blocker.

Stationary initialization can use either the global motion-inhibit gate or a
short-lived `attest_fixed_rig_stationary` Provider request. The latter is
accepted only after explicit operator confirmation that the camera and IMU are
rigidly fixed and stationary. It is bounded to 120 seconds, does not reset the
VIO epoch, and does not revoke or interfere with an arm-controller lease.

## Documentation

- [Validation](VALIDATION.md) — stopped checks and remaining live/deployment
  qualification; for operators, installers, and reviewers.
- [Changelog](CHANGELOG.md) — release history and migration notes; for
  maintainers and coding agents investigating a behavior change.
