# Orbbec Femto Bolt Resource Provider 0.3.1

This replaceable package exposes the camera's complete practical sensory data plane through the Resource Provider Manager and World State Fabric while preserving explicit operator start and stop control.

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

## Release status

Version 0.3.1 is the current integrated source baseline. The native C++ paths, shared-memory layout v2, IR, aligned-depth, point-cloud, metadata, transforms, and calibration utility must be built and verified on the target Windows/Femto Bolt machine before they are used as a safety-relevant perception source.

## Install or replace

Stop the workspace, delete only `providers\orbbec_femto_bolt`, then extract the ZIP into `C:\Projects\testing_physical_ai`.

Use Developer PowerShell for Visual Studio 2022:

```powershell
.\providers\orbbec_femto_bolt\scripts\setup.ps1
```

The source package does not include the complete Orbbec development SDK. Setup locates SDK 2.8.6, builds CameraHost, copies the runtime DLL and extension tree, validates depth dependencies, installs the Python package, and updates only the camera entry in `config\providers.json`.

## Run and verify

```powershell
.\platform_core\scripts\run_workspace.ps1
Start-Sleep -Seconds 20
.\providers\orbbec_femto_bolt\scripts\verify.ps1 -WaitSeconds 90
```

Verification writes RGB, native depth, IR, color-aligned depth, point-cloud PLY, and a JSON report containing calibration, IMU, timestamps, metadata, stream catalog, and Manager capabilities under `captures`.

Frame metadata may require a separate Administrator registration step. See `docs\WINDOWS_FRAME_METADATA_SETUP.md`.

See `docs\FULL_CAPABILITY_PROFILE.md` for streams, coordinate frames, readiness, flags, and intentionally separate device-control functions.

## v0.3 device calibration and transforms

On first sighting of a valid Femto Bolt serial, the Provider creates a device-bound accelerometer calibration under `config/calibration/devices/orbbec/femto-bolt/<serial>/imu-accelerometer.json`. The initial `UNCALIBRATED` correction is identity scale and zero offset. The Provider applies the effective correction to canonical accelerometer observations and publishes its revision.

The Provider also publishes static depth-to-color and available IMU-to-color transform observations. Its shared-memory reader exposes retained ordered IMU samples for VIO consumers with gap detection.


## Standalone accelerometer calibration GUI

With the workspace running and the camera Provider `HOT`, launch:

```powershell
.\providers\orbbec_femto_bolt\scripts\run_accelerometer_calibration.ps1
```

The local GUI captures two seconds of raw accelerometer samples in six strong axis orientations, solves diagonal scale and offset, backs up the previous device-specific JSON, writes the new `CUSTOM_CALIBRATED` revision, and requests live Provider reload. See `docs/ACCELEROMETER_CALIBRATION_GUI.md`.
