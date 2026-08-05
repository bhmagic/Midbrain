# Changelog

## Unreleased

- Republished calibration and static camera transforms while HOT so a restarted
  in-memory Fabric can recover without restarting the camera.
- Added explicit native optical convention and `camera_system_x/y/z` metadata
  across calibration, routes, observations, and device information.
- Added the flexible generic shared-memory route while retaining the
  Orbbec-specific reader as an explicit compatibility fallback.
- Added a headless external launcher, current mapping-name configuration, and
  regression isolation from unrelated top-level Python modules.

## 0.3.1

- Added the attended six-position accelerometer calibration GUI, quality
  checks, nonlinear refinement, atomic device-specific replacement, backup,
  and live reload.

## 0.3.0

- Added serial-bound persistent accelerometer calibration, retained IMU
  history, calibration status/revision, and static camera/IMU transforms.

## 0.2.0

- Expanded the hardware-confirmed RGB-D path to RGB, native and registered
  depth, IR, IMU, point cloud, calibration, timing, metadata, device identity,
  shared-memory layout v2, and capability-specific readiness.
- Kept RGB point clouds opt-in and separated state-changing device controls
  from the always-on sensory data plane.
