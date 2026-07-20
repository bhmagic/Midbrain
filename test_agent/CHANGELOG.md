# Changelog

## 0.2.9

- Show initialization accel/gyro window counts and inferred sample rates in the Pose propagation card.

## 0.2.8

- Show accelerometer/gyroscope history counts, timestamp skew, and the current VIO initialization blocker.
- Mark an earlier failed startup auto-init as superseded while a later manual initialization is running or has succeeded.

## 0.2.7

- Updated the GUI for inertial-first VIO semantics.
- Renamed the visual state to Visual Correction and the pose state to Pose Propagation.
- Displays whether RGB-D or synchronized IR/depth supplied the accepted correction.
- Displays visual update age, correction magnitude, reprojection error, and acceptance state.
- Displays IMU propagation step count and inertial state timestamp.
- Retained gravity, reset, orthographic isometric point-cloud, camera-frustum, and map-capture diagnostics.

## 0.2.6

- Waits for the VIO Provider to acknowledge motion inhibit before requesting initialization.
- Recovers startup initialization when reset changed epoch but the Manager returned a transient control-response error.
- Added a separate rotation-estimator status light with visual/gyro disagreement and gyro sample diagnostics.
- Distinguishes visual tracking, gyro rotation hold, gravity adjustment, and map-capture states.
