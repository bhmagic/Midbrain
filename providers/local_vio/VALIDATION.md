# Validation

## v0.2.2

- Regression: default 80-sample initialization succeeds with 50 Hz accel and gyro histories.
- Initialization windows are selected by count and report approximately 50 Hz.
- Full Local VIO suite: 30 tests passed.


Validated in the delivery environment:

- 30 Local VIO regression tests.
- Stationary inertial propagation holds pose within numerical tolerance.
- Fast yaw propagates from gyroscope samples without visual frames.
- RGB-D visual pose is applied as an error-state correction rather than direct state replacement.
- Healthy RGB-D is preferred over IR.
- IR can replace a weak RGB correction when it has materially stronger support.
- Reset acceptance remains independent of immediate Fabric status publication.
- Observation sequence remains monotonic across session resets.
- Quiet-IMU gravity gating and bounded rotation-only leveling remain active.
- Adaptive raw/circular-LCN feature selection remains covered.
- Python source compilation.
- Browser JavaScript syntax validation for the associated GUI.

Still required on the physical Bolt:

- Automatic startup initialization with the new inertial state.
- Long-run stationary velocity and position drift.
- Fast-pivot orientation accuracy against an external reference.
- RGB-D correction quality in the dim target room.
- IR fallback frequency, timestamp alignment, and geometric accuracy.
- Camera/IMU time-offset measurement.
- Comparison against a native Basalt or OpenVINS-class backend on recorded data.


## v0.2.1

- Regression test: initialization succeeds when the supplied RGB timestamp is unrelated to the IMU timestamps.
- Provider test: video and IMU internal timestamp helpers consistently prefer SDK system timestamps.
- Status diagnostics include separate accelerometer and gyroscope history counts, timestamp skew, and initialization blocker.
