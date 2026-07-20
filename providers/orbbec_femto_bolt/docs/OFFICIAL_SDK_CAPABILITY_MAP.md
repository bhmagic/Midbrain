# Orbbec SDK Capability Map for Femto Bolt

This file separates the full sensory data plane from state-changing administration.

| Orbbec SDK area | 0.2.0 status | Platform representation |
|---|---|---|
| Color stream | Implemented | `camera.rgb.frame_ref` |
| Depth stream | Implemented | `camera.depth.frame_ref` |
| Infrared stream | Implemented | `camera.ir.frame_ref` |
| Hardware D2C request | Implemented | Camera configuration/device info |
| Software D2C alignment | Implemented | `camera.depth_aligned_to_rgb.frame_ref` |
| Depth point cloud | Implemented | `camera.point_cloud.xyz.frame_ref` |
| RGBD point cloud | Experimental | `camera.point_cloud.xyzrgb.frame_ref` |
| Accelerometer | Implemented | `camera.imu.accel` |
| Gyroscope | Implemented | `camera.imu.gyro` |
| RGB/depth/IR intrinsics and extrinsics | Implemented where exposed | `camera.calibration` |
| IMU intrinsics and camera extrinsics | Implemented where exposed | `camera.calibration` |
| Device/system/global timestamps | Implemented | Every frame/sample observation |
| Per-frame metadata | Implemented when registered/exposed | BufferRef `frame_metadata` |
| Firmware, UID, connection, USB identity | Implemented | `camera.device_info` |
| Stream layout and dropped counters | Implemented | `camera.device_info` |
| Timestamp-nearest selection | Implemented in Fabric | `/v1/sync` |
| Stream discovery | Implemented in Fabric | `/v1/streams` |
| Capability discovery | Implemented in Manager | `/v1/capabilities` |
| Recording/playback | Deferred | Recorder/playback Provider |
| Triggered capture | Deferred | Explicit provider command |
| Device-time synchronization | Deferred | Time-sync command and clock observation |
| Multi-camera synchronization | Deferred | Coordination Provider/Skill |
| Camera property controls | Deferred | Versioned commands with range discovery |
| Post-processing controls | Deferred | Profile or derived processing Provider |
| Firmware update | Deferred | Offline operator-administered tool |
| Hot-plug recovery | Partial | Manager heartbeat expiry; restart policy remains |

The default package exposes the full practical runtime sensor output without granting an agent silent authority over device-changing administrative operations.
