# CameraHost 0.2 Pipeline Summary

CameraHost is the native producer for the Femto Bolt Resource Provider. It publishes RGB, depth, IR, aligned depth, point cloud, IMU, calibration, frame metadata, device information, and status through shared-memory layout version 2.

## Processing order

1. Open one Orbbec device and record identity, firmware, connection, USB identifiers, and global-timestamp support.
2. Select default RGB/depth/IR profiles and request hardware D2C-compatible depth when available.
3. Request SDK frame synchronization and host-domain global timestamps.
4. Publish raw video frames with device/system/global timestamps and available SDK frame metadata.
5. Run software D2C alignment and publish a separately labeled depth image resampled into color coordinates.
6. Apply the active depth scale and publish XYZ or experimental XYZRGB point data.
7. Run the IMU pipeline and publish accelerometer/gyroscope samples.
8. Publish RGB/depth/IR calibration, IMU intrinsics, and available inter-sensor extrinsics; retry after depth starts if initial calibration is provisional.

## Lifecycle

The host exits on console cancellation and does not wait for interactive stdin. The Python Resource Provider owns its process, exposes HOT/WARM/stop controls, and is the intended launcher during normal workspace operation.

## Validation boundary

The source, layouts, Python readers, and package structure are statically validated before packaging. The updated C++ must still be compiled with MSVC and tested against Orbbec SDK 2.8.6, installed firmware, Windows metadata registration, and the physical Femto Bolt.
