# Local VIO Resource Provider v0.3.0

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



### v0.2.2 sample-rate-independent startup

Startup no longer assumes that 80 IMU samples fit inside 1.5 seconds. The initializer selects the newest 80 accelerometer and 80 gyroscope samples before the common IMU timestamp and accepts them when their span is no more than five seconds. This supports the Femto Bolt at 50 Hz, where 80 samples require about 1.58 seconds. Status includes the selected window counts and inferred sample rates.

Stationary initialization can use either the global motion-inhibit gate or a
short-lived `attest_fixed_rig_stationary` Provider request. The latter is
accepted only after explicit operator confirmation that the camera and IMU are
rigidly fixed and stationary. It is bounded to 120 seconds, does not reset the
VIO epoch, and does not revoke or interfere with an arm-controller lease.

### v0.2.1 startup timestamp fix

Initialization uses a common IMU timestamp window and prefers SDK system timestamps consistently across video, accelerometer, and gyroscope inputs. The status stream exposes IMU history counts, timestamp skew, and an explicit initialization blocker.
