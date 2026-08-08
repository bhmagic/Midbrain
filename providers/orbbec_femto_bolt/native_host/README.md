# CameraHost native process

`CameraHost.exe` is the only process that opens the Orbbec Femto Bolt. It owns the SDK pipelines and publishes high-volume payloads into Windows named shared memory. Python, Skills, and UI consumers use BufferRefs and do not reopen the camera.

## Default outputs

- RGB
- Native metric depth
- Infrared
- Accelerometer and gyroscope
- Software depth aligned/resampled into color coordinates
- Metric XYZ point cloud
- RGB/depth/IR/IMU calibration where exposed
- Per-frame SDK metadata
- Device, system, and global timestamps
- Device identity and status

Software D2C output retains the source depth frame's device, system, and global
capture timestamps. Its own output frame number still identifies the registered
product. Processing-completion time is not published as sensor capture time.

Default mapping:

```text
Local\FemtoBoltPipeline_CameraHost_v2
```

## Run directly

```powershell
.\scripts\run_host.ps1
```

Useful switches:

| Switch | Effect |
|---|---|
| `-MappingName <name>` | Select the Windows named mapping |
| `-NoColor` | Disable RGB |
| `-NoDepth` | Disable native depth and point cloud |
| `-NoIr` | Disable infrared |
| `-NoImu` | Disable accelerometer and gyroscope |
| `-NoFrameSync` | Disable SDK frame-sync request |
| `-NoHardwareD2C` | Disable hardware D2C profile request |
| `-NoAlignedDepth` | Disable software depth-to-color product |
| `-NoPointCloud` | Disable point-cloud generation and allocation |
| `-RgbPointCloudExperimental` | Request `OB_FORMAT_RGB_POINT` instead of XYZ |

## Shared-memory contract

The binary layout is defined in `include\FemtoBoltPipeline\FemtoSharedMemoryLayout.hpp`. Layout version 2 supports nine streams, a 4096-byte mapping header, and 512-byte slot headers carrying timestamps and per-frame metadata. Every slot uses a seqlock generation:

1. Read the generation.
2. Reject odd generations.
3. Copy metadata and payload.
4. Read the generation again.
5. Accept only when both reads are equal and even.

A BufferRef is short-lived. Consumers must reacquire a reference when a ring slot is recycled.

## Point-cloud behavior

The default `POINT` payload is an `OBPoint[]` array with XYZ values in millimetres in the depth optical frame. The SDK filter receives the current depth scale. RGB point clouds are opt-in because continuous `RGB_POINT` behavior must be validated for each Femto Bolt/SDK/firmware combination.

## Runtime package

Setup copies `OrbbecSDK.dll` and the complete SDK `extensions` directory next to `CameraHost.exe`. Depth initialization depends on the frame-processor and depth-engine extensions. `scripts\diagnose_runtime.ps1` validates the required files before launch.
