# Changelog

## 0.2.3 - 2026-07-29

- Stopped treating a cached Fabric `camera.rgbd.bundle` ring reference as
  reusable image storage. Fabric metadata remains cached briefly, but each
  visual iteration now obtains the current provider-local RGB and
  aligned-depth references from the already-open shared-memory mapping.
- Copies depth and RGB immediately, retries a recycled slot at most four
  times, and rejects missing timestamps or pairs outside the bundle's declared
  synchronization threshold.
- Keeps the optional IR correction path nonblocking when its latest
  shared-memory slot recycles.
- Added regression coverage for replacing stale Fabric references with current
  provider-local references. Live validation recovered a current tracking pose
  without requiring Fabric to decide Skill-specific freshness.

## 0.2.2

- Fix an impossible startup gate at 50 Hz: 80 required samples no longer have to fit inside a fixed 1.5-second window.
- Select the newest fixed-count accelerometer and gyroscope windows in the common IMU time domain, with a five-second stale-history ceiling.
- Publish initialization window counts and inferred accel/gyro sample rates.
- Add a 50 Hz, 80-sample regression test matching the Femto Bolt hardware report.

## 0.2.1

- Fix startup initialization by selecting accelerometer and gyroscope stationarity windows in the common IMU timestamp domain instead of around the RGB timestamp.
- Prefer SDK system timestamps consistently for video and IMU internal VIO processing.
- Add explicit IMU-history, timestamp-skew, and initialization-blocker diagnostics.

## 0.2.0

- Replaced the visual-first pose loop with a 15-state inertial error-state filter.
- Propagates orientation, position, velocity, gyroscope bias, accelerometer bias, and covariance from every ordered IMU sample.
- Added high-rate non-committing pose prediction between visual updates.
- Converted RGB-D odometry into a gated metric correction measurement rather than direct pose authority.
- Added optional synchronized IR plus native-depth correction for weak or dark RGB scenes.
- Added pose covariance, estimated bias, correction magnitude, visual staleness, and propagation diagnostics.
- Preserved the established adaptive circular local-contrast-normalization frontend.
- Preserved quiet-IMU gravity leveling, including the adaptive gyro noise-floor gate.
- Preserved reset epoch, monotonic sequence, startup motion-inhibit, and point-cloud lifecycle fixes.

## 0.1.6

- Made reset acceptance independent of immediate Fabric status publication.
- Added full 3D visual/gyro rotation disagreement instead of scalar-angle-only checks.
- Added gyro-seeded iterative PnP as a secondary candidate while retaining raw EPNP as baseline.
- Added short gyro rotation propagation with translation hold when visual rotation is unavailable or untrustworthy.
- Marked gyro-only propagation as `DEGRADED` so mapping pauses instead of accepting corrupted points.
- Added rotation source, disagreement angle, gyro sample count, and gyro step diagnostics.
