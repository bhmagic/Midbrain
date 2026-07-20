# Femto Bolt Full Sensory Capability Profile — 0.2.0

This profile exposes the complete practical sensor output while preserving explicit operator start and stop control.

## Published products

| Product | Fabric stream | Payload location |
|---|---|---|
| RGB | `camera.rgb.frame_ref` | Shared memory |
| Native depth | `camera.depth.frame_ref` | Shared memory |
| Infrared | `camera.ir.frame_ref` | Shared memory |
| Depth aligned to RGB | `camera.depth_aligned_to_rgb.frame_ref` | Shared memory |
| XYZ point cloud | `camera.point_cloud.xyz.frame_ref` | Shared memory |
| Experimental XYZRGB cloud | `camera.point_cloud.xyzrgb.frame_ref` | Shared memory |
| Accelerometer | `camera.imu.accel` | Inline observation |
| Gyroscope | `camera.imu.gyro` | Inline observation |
| Paired IMU | `camera.imu.bundle` | Inline observation |
| Calibration | `camera.calibration` | Inline observation |
| Device/profile information | `camera.device_info` | Inline observation |
| Provider RGB-D bundle | `camera.rgbd.bundle` | BufferRefs only |
| Status | `camera.status` | Inline observation |

Per-frame metadata is attached to each video BufferRef rather than published as a duplicate high-rate stream.

## Four layers

1. **Raw sensors:** RGB, native depth, IR, accel, gyro.
2. **Calibrated geometry:** RGB/depth/IR intrinsics and distortion, depth-to-color geometry, IMU intrinsics, and available inter-sensor extrinsics.
3. **Synchronized selection:** provider RGB-D/IMU bundles and Fabric timestamp-nearest multi-stream lookup.
4. **Derived spatial products:** depth resampled into color coordinates and point clouds.

## BufferRef timing and metadata

Each v2 video BufferRef can include:

- Device timestamp
- System timestamp
- Host-domain global timestamp
- SDK frame number and format
- Exposure, gain, white balance, frame-rate, ROI, laser, HDR, and other available frame metadata
- Calibration revision
- Coordinate frame
- Shared-memory generation and slot information

`flags` uses these bits:

| Bit | Meaning |
|---:|---|
| `0x1` | Per-frame metadata is present |
| `0x2` | Global timestamp is non-zero |
| `0x4` | Frame is explicit depth aligned/resampled into color coordinates |
| `0x8` | Point cloud contains experimental RGB point data |

Windows may require vendor metadata registration. Absence of frame metadata does not invalidate raw RGB-D operation.

## Coordinate frames

- `femto_bolt_color_optical_frame`
- `femto_bolt_depth_optical_frame`
- `femto_bolt_ir_optical_frame`
- `femto_bolt_imu_frame`

These identifiers label observations. The future World State Fabric transform graph must connect them to robot and world frames.

## Point-cloud modes

- `xyz`: default `OBPoint[]`, XYZ millimetres, depth optical coordinates.
- `xyzrgb`: opt-in experimental `OBColorPoint[]`, generated from aligned RGB-D data.
- `off`: releases point-cloud allocation and processing cost.

Configure the mode after `--point-cloud-mode` in `config\providers.json`.

## Readiness semantics

Provider `ready` requires live RGB and native depth. Every optional product has independent capability readiness. Missing geometry, IR, IMU, aligned depth, or point cloud may mark health `DEGRADED` without hiding working raw streams. Frame metadata and global timestamps are reported independently because their availability depends on operating-system registration, firmware, and device support.

## Device-control boundary

The SDK also exposes exposure/property controls, post-processing, presets, recording/playback, external trigger, device-time synchronization, multi-camera synchronization, and firmware operations. These functions change device state, consume storage/resources, or require deployment-specific wiring. They are documented for explicit future command contracts rather than silently enabled by the always-on sensory profile.
