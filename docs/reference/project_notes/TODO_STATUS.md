# TODO Status — Inertial-First VIO Reference Backend v0.3.10

## Hardware-verified baseline

- [x] Manager/Fabric release build on Windows.
- [x] Femto Bolt RGB, aligned depth, native depth, IR, point cloud, and ordered IMU operation.
- [x] World-frame RGB point cloud can scan a room during moderate pivoting.
- [x] Forced reinitialization resumes after the sequence monotonicity fix.
- [x] Gravity-down recovery is observable and timing has been tuned on hardware.
- [x] Orthographic isometric visualization is usable.
- [x] Six-orientation accelerometer calibration GUI is packaged with the camera Provider.

## Implemented in v0.3.8 source

- [x] Replaced visual-first pose authority with a 15-state inertial error-state filter.
- [x] Every ordered IMU sample propagates orientation, position, velocity, biases, and covariance.
- [x] Added high-rate inertial prediction between camera frames.
- [x] RGB-D is a metric correction measurement rather than direct pose replacement.
- [x] Added innovation and Mahalanobis gates for visual corrections.
- [x] Added synchronized IR plus native-depth fallback for weak RGB scenes.
- [x] Retained adaptive circular local contrast normalization.
- [x] Retained quiet-IMU gravity roll/pitch leveling without translation or yaw mutation.
- [x] GUI separates inertial propagation from RGB-D/IR correction.
- [x] Startup reset, motion-inhibit acknowledgement, epoch, and map lifecycle fixes remain included.

## Required target validation

- [ ] Verify automatic startup initialization reaches `SUCCEEDED` with Local VIO v0.2.2.
- [ ] Verify high-rate gyro propagation during fast pivots.
- [ ] Measure stationary position/velocity drift.
- [ ] Measure drift during short visual occlusion and correction after reacquisition.
- [ ] Confirm RGB-D updates reduce inertial drift without abrupt pose replacement.
- [ ] Confirm IR fallback is synchronized and improves dim-room correction quality.
- [ ] Measure camera/IMU time offset and uncertainty.
- [ ] Add deterministic recording/replay and trajectory metrics.
- [ ] Compare against native Basalt and OpenVINS/MSCKF evaluation backends.


## v0.3.10 validation target

- [ ] Confirm startup auto-initialization on the physical Femto Bolt at its observed approximately 50 Hz IMU rate.
- [ ] Confirm the GUI reports initialization windows `80/80` and inferred rates near `50/50 Hz`.
- [ ] Confirm the initialization blocker clears to `none`, the Skill reaches `SUCCEEDED`, and IMU propagation steps increase.
- [ ] Record actual RGB/accelerometer/gyroscope system/global/device timestamp availability for formal clock-domain metadata.
