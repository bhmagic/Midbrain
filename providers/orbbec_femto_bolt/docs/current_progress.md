# Current Provider Progress — 0.2.0

## Implemented sensory data plane

- RGB, native metric Y16 depth, and infrared frames.
- Hardware D2C profile request when supported by the SDK/device.
- Software depth-to-color output with explicit aligned-depth semantics.
- Metric XYZ point cloud in shared memory.
- Opt-in experimental XYZRGB point cloud.
- Accelerometer and gyroscope samples plus paired IMU bundles.
- RGB/depth/IR intrinsics and distortion, depth-to-color geometry, IMU intrinsics, and available camera/IMU extrinsics.
- Device, firmware, connection, USB, SDK, mapping-layout, stream, dropped-frame, and timestamp metadata.
- Per-frame SDK metadata attached to BufferRefs when Windows registration and the stream expose it.
- Host-domain global timestamps when device/firmware support them.
- Provider RGB-D bundles and Fabric timestamp-nearest lookup.
- Capability-specific readiness and operator-controlled HOT/WARM/stop lifecycle.
- Windows named shared memory v2 with generation-validated BufferRefs.

## Deliberately separate administrative capabilities

Recording/playback, triggered capture, time/multi-camera synchronization configuration, camera-property tuning, presets, firmware update, and post-processing controls are not always-on sensory streams. They should be added as explicit commands or separate Providers with permissions, deadlines, auditability, and operator policy.

## Remaining validation and future work

- Build and test 0.2.0 on the Windows/Femto Bolt machine.
- Confirm continuous IR, aligned-depth, XYZ point-cloud, global-timestamp, and metadata behavior.
- Quantify sustained CPU, memory, USB, dropped-frame, and latency costs.
- Decide whether XYZRGB is stable enough to leave experimental status.
- Add device controls under a formal command contract with range discovery and rollback.
- Add trigger/time-sync/multi-camera configuration contracts.
- Add recording/playback as independent lifecycle-managed capabilities.
- Add a signed prebuilt runtime package after redistribution review.
