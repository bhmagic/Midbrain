# Official Orbbec References Used

The 0.2.0 sensory expansion was designed from Orbbec's public SDK v2 documentation and the SDK 2.8.6 interfaces already installed on the target workstation.

- Orbbec SDK v2 repository and Windows metadata-registration requirement:
  https://github.com/orbbec/OrbbecSDK_v2
- Orbbec SDK v2 application guide (video, IMU, calibration, D2C/C2D, point clouds, recording, synchronization, controls, metadata):
  https://orbbec.github.io/docs/OrbbecSDKv2_API_User_Guide/source/3_Application_Guide/Application_Guide.html
- Orbbec SDK v2 examples catalog (IR, IMU, sync/alignment, point clouds, metadata, recording, controls, hot-plug):
  https://github.com/orbbec/OrbbecSDK_v2/blob/main/examples/README.md
- Femto Bolt documentation index:
  https://doc.orbbec.com/documentation/Orbbec%20Femto%20Bolt%20Documentation
- Femto Bolt product specifications:
  https://www.orbbec.com/products/tof-camera/femto-bolt/

The source package does not redistribute the complete Orbbec development SDK. Build and runtime dependency details are in `SDK_REQUIREMENTS.md` and `sdk_distribution.md`.

## Scope decision

This release exposes the full practical always-on sensory data plane: RGB, native depth, IR, IMU, calibration, aligned depth, point clouds, timing, metadata, and device identity. State-changing or operator-sensitive functions such as exposure controls, presets, recording, external triggers, multi-camera synchronization, and firmware update are documented but intentionally deferred to explicit command contracts.
