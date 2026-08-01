# VIO and Sensor-Fusion Design

## Intended behavior

The target is the architecture used by stable low-latency VR/XR tracking: the IMU is the high-rate motion model and visual sensing prevents drift. Camera frames are slower and can blur or lose texture during rapid movement; they should not be the only motion clock.

## Current filter state

Local VIO v0.3.0 uses a 15-state error-state filter with nominal state:

- Orientation `R` or quaternion equivalent.
- Position `p`.
- Velocity `v`.
- Gyroscope bias `b_g`.
- Accelerometer bias `b_a`.

The covariance tracks three errors for each of orientation, position, velocity, gyro bias, and accelerometer bias.

Every new epoch uses `MIDBRAIN_X_FORWARD_Y_LEFT_Z_UP_V2`: positive X is the
initial camera/body forward direction projected onto the gravity-horizontal
plane, positive Y is left, and positive Z is opposite gravity. The world
gravity vector is `[0, 0, -g]`.

Raw camera optical coordinates are not converted by renaming axes. They remain
positive X image-right, positive Y image-down, and positive Z optical-forward.
Calibrated transforms connect that sensor-native frame to VIO and other
three-dimensional frames.

## Inertial propagation

Every retained ordered accelerometer/gyro sample propagates:

- Orientation from bias-corrected angular rate.
- Specific force from body to world coordinates.
- Velocity using world acceleration plus gravity.
- Position from velocity and acceleration.
- Bias and state uncertainty.

A non-committing fast-prediction path can publish poses between camera updates. The default Provider polling interval is 5 ms and configured inertial publication is 100 Hz, subject to available sensor data and host scheduling.

## RGB-D correction

RGB plus RGB-aligned depth is the preferred correction source. The visual frontend retains:

- Raw grayscale ORB extraction first.
- Descriptor matching.
- Metric 3D geometry from depth.
- EPNP and gyro-seeded iterative PnP candidates.
- Innovation size limits and Mahalanobis gating before filter correction.

The raw path remains the baseline. A visual correction changes the propagated state through the filter update; it does not overwrite pose directly.

## Circular local-contrast normalization

Dim-room tracking can have weak visible-light features. Adaptive circular local-contrast normalization uses a disk-shaped local kernel to estimate local mean and variance, then applies bounded contrast gain before ORB extraction.

The baseline guarantee is important:

- Raw ORB/PnP runs first.
- Normalization is only an additional candidate in dim or low-contrast conditions with weak raw features.
- A healthy raw correction is retained.
- A normalized candidate must materially outperform raw support before selection.

The Provider can be configured with `raw_baseline` for direct A/B comparison.

## IR plus depth fallback

IR may provide useful texture when RGB is weak. It is not an independent pose authority. It is eligible only when:

- IR fallback is enabled.
- IR intrinsics and required extrinsics exist.
- IR, native depth, and the current RGB correction epoch are timestamp-compatible.
- The IR/depth result is stronger than the available RGB-D correction.

Native depth is nearest-neighbor resampled when IR and depth resolutions differ. IR and depth are slower/lower-resolution visual measurements and remain corrections to the high-rate inertial state.

## Gravity leveling

The gravity behavior was tuned during the visual-first development and intentionally retained:

- Startup estimates gyro zero-rate bias and residual noise.
- Nominal quiet threshold is `0.012 rad/s`.
- Effective threshold is `max(0.012, 1.5 × robust noise ceiling)`, clamped to `0.008..0.030 rad/s`.
- Quiet gating uses bias-corrected gyro statistics and accelerometer magnitude/direction stability.
- Gravity modifies roll and pitch only.
- Yaw and translation are preserved.
- Correction is bounded; normal tracking uses a smaller correction than degraded recovery.
- GUI states are `OFF`, `READY`, and `ACTIVE`.

The V2 world-basis migration changes only estimator/world interpretation.
Hardware accelerometer calibration, axis metadata, bias/scale values,
calibration revision, and camera/IMU extrinsics remain intact.

## Status interpretation

- **Pose Propagation** reports inertial initialization, propagation steps, and state timestamp.
- **Visual Correction** reports sensor source, matches, inliers, correction age, reprojection error, and acceptance.
- **Rotation Estimator** reports visual/gyro consistency or inertial propagation source.
- **Gravity Adjustment** reports whether the quiet gate is unavailable, ready, or actively leveling.
- **Map Capture** reports whether points are being inserted or why capture is paused.

## Classification and limits

This is a correct inertial-propagation/visual-update reference architecture, but not yet a production-grade feature-level VIO implementation. Visual measurements are frame-to-frame pose corrections, not an MSCKF feature-track update or Basalt fixed-lag nonlinear optimization. Long visual outages can still produce inertial position drift. Camera/IMU time offset is not estimated online.
