# Orbbec Femto Bolt Resource Provider

This replaceable package exposes the camera's complete practical sensory data plane through the Resource Provider Manager and World State Fabric while preserving explicit operator start and stop control.

`manifest.json` is authoritative for Provider identity, version, advertised
capabilities, streams, transport routes, and readiness metadata. This README
explains how to install and operate the package; it does not duplicate the
complete manifest inventory.

## Default sensory profile

- RGB
- Native metric Y16 depth
- Infrared
- Accelerometer and gyroscope
- Valid RGB, depth, IR, and IMU calibration where exposed by the SDK
- SDK frame synchronization request
- Hardware depth-to-color profile request when supported
- Explicit software depth resampled into RGB coordinates
- Metric XYZ point cloud
- Per-frame metadata when registered and exposed by Windows/SDK
- Host-domain global timestamps when supported
- Device identity, firmware, connection, USB identifiers, stream layout, and dropped-frame counters
- Provider RGB-D and IMU bundles

Large payloads remain in Windows named shared memory. The Fabric carries generation-validated `BufferRef` metadata only.

Native RGB, depth, and infrared geometry remains in the camera optical
convention: X image-right, Y image-down, and Z optical-forward. Cross-component
metadata names those components `camera_system_x`, `camera_system_y`, and
`camera_system_z` and carries
`CAMERA_OPTICAL_X_RIGHT_Y_DOWN_Z_FORWARD_V1`. This does not change the
device-bound accelerometer calibration or its hardware-axis metadata.

The provider publishes one atomic `camera.rgbd.data_routes` route-set
observation. It contains both the generic flexible-grid shared-memory
descriptor and the Orbbec-specific named-memory reader. The generic route is
preferred when the consumer supports it; the branded route remains an
explicit-provider compatibility fallback. Publishing both in one observation
prevents the Fabric's latest-value stream semantics from hiding either route.

The generic descriptor does not require RGB, IR, native depth, or registered
depth to share resolution, aspect ratio, valid boundary, or capture timestamp.
It carries provider-written alignment metadata, including custom valid-region
information. Discovery goes through the Fabric, while all large RGB-D payloads
remain in shared memory.

## Hardware qualification

Build and verify the native paths, shared-memory layout, optical metadata, IR,
aligned depth, point cloud, and calibration extensions on the target Windows
and Femto Bolt installation before treating them as safety-relevant
perception. [Validation](VALIDATION.md) owns the current software-versus-live
evidence boundary; [Changelog](CHANGELOG.md) owns release history.

## Install or replace

Stop the workspace, replace only `providers\orbbec_femto_bolt`, and preserve
the rest of the repository plus machine-local `config`. The package path must
remain `providers\orbbec_femto_bolt`; no fixed absolute workspace path is
required.

Use Developer PowerShell for Visual Studio 2022:

```powershell
.\providers\orbbec_femto_bolt\scripts\setup.ps1
```

The source package does not include the complete Orbbec development SDK. Setup
locates the supported SDK installation, builds CameraHost, copies the required
runtime DLL and extension tree, validates depth dependencies, installs the
Python package, and updates only the camera entry in
`config\providers.json`. See [SDK requirements and distribution](docs/SDK_REQUIREMENTS.md).

## Run and verify

```powershell
.\platform_core\scripts\run_workspace.ps1
Start-Sleep -Seconds 20
.\providers\orbbec_femto_bolt\scripts\verify.ps1 -WaitSeconds 90
```

Verification writes RGB, native depth, IR, color-aligned depth, point-cloud PLY, and a JSON report containing calibration, IMU, timestamps, metadata, stream catalog, and Manager capabilities under `captures`.

Frame metadata may require a separate Administrator registration step. See `docs\WINDOWS_FRAME_METADATA_SETUP.md`.

The manifest and Provider status are the current source for advertised streams,
coordinate metadata, readiness, and transport flags. State-changing device
functions remain deliberately outside the active interface; their intended
boundaries are summarized in
[SDK requirements and distribution](docs/SDK_REQUIREMENTS.md#deliberately-excluded-device-control-scope).

## Device calibration and transforms

On first sighting of a valid Femto Bolt serial, the Provider creates a device-bound accelerometer calibration under `config/calibration/devices/orbbec/femto-bolt/<serial>/imu-accelerometer.json`. The initial `UNCALIBRATED` correction is identity scale and zero offset. The Provider applies the effective correction to canonical accelerometer observations and publishes its revision.

The Provider also publishes static depth-to-color and available IMU-to-color transform observations. Its shared-memory reader exposes retained ordered IMU samples for VIO consumers with gap detection.


## Standalone accelerometer calibration GUI

With the workspace running and the camera Provider `HOT`, launch:

```powershell
.\providers\orbbec_femto_bolt\scripts\run_accelerometer_calibration.ps1
```

The local GUI captures two seconds of raw accelerometer samples in six strong axis orientations, solves diagonal scale and offset, backs up the previous device-specific JSON, writes the new `CUSTOM_CALIBRATED` revision, and requests live Provider reload. See `docs/ACCELEROMETER_CALIBRATION_GUI.md`.

## Documentation

Human and installation-agent entry points:

- [SDK requirements and distribution](docs/SDK_REQUIREMENTS.md) — build and
  runtime dependencies, vendor references, redistribution policy, and deferred
  device-control scope.
- [Windows frame-metadata setup](docs/WINDOWS_FRAME_METADATA_SETUP.md) — the
  separate Administrator registration step when frame metadata is needed.
- [Accelerometer calibration GUI](docs/ACCELEROMETER_CALIBRATION_GUI.md) —
  attended six-position calibration and recovery.
- [Validation](VALIDATION.md) — stopped coverage and live checks still
  required on the installed device.

Coder and coding-agent references:

- [CameraHost](native_host/README.md) — native process ownership,
  shared-memory layout, and BufferRef behavior.
- [`manifest.json`](manifest.json) — authoritative capabilities, streams,
  route descriptors, version, and readiness metadata.

History:

- [Changelog](CHANGELOG.md) — release history; not a current setup guide.
