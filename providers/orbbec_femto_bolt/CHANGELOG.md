# Changelog

## 0.2.0

- Expands the hardware-confirmed 0.1.6 RGB-D path into a full sensory profile.
- Enables RGB, native depth, infrared, accelerometer, gyroscope, frame synchronization, hardware D2C request, software depth-to-color alignment, and XYZ point clouds by default.
- Adds shared-memory layout v2 with explicit aligned-depth and point-cloud streams.
- Adds per-frame SDK metadata, host-domain global timestamps, firmware/connection/USB identity, and transport flags.
- Publishes RGB/depth/IR calibration, depth-to-color geometry, IMU intrinsics, and camera/IMU extrinsics where the SDK exposes them.
- Publishes device information, calibration revisions, coordinate-frame identifiers, synchronized RGB-D bundles, and paired IMU bundles.
- Adds capability-specific readiness for each camera product.
- Extends verification to RGB, native depth, IR, aligned depth, point-cloud PLY, calibration, IMU, metadata, Manager capabilities, Fabric stream discovery, and timestamp-nearest synchronization.
- Keeps XYZRGB point clouds opt-in and experimental.
- Documents Windows frame-metadata registration and separates state-changing device controls from the always-on sensory data plane.

## 0.3.0

- Added serial-bound persistent accelerometer calibration with identity first-seen fallback.
- Added calibrated canonical accelerometer publication and reload control.
- Added calibration revision/status publication.
- Added static camera/IMU transform publication.
- Added ordered retained shared-memory IMU history access.


## 0.3.1

- Added a standalone local six-position accelerometer calibration GUI.
- Added raw shared-memory IMU capture with two-second averaging for x+/x-/y+/y-/z+/z-.
- Added the first-order six-unknown solve and damped nonlinear refinement.
- Added capture quality, conditioning, and residual checks.
- Added atomic device-specific calibration replacement with timestamped backup and Provider reload.
- Added operator documentation and synthetic solver tests.
