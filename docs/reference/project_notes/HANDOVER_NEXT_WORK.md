# Project Handover and Recommended Next Work

## v0.3.10 50 Hz initialization fix

The physical log showed 1,198 accelerometer and 1,198 gyroscope samples with zero timestamp skew, yet initialization remained at `WAITING_FOR_RECENT_ACCELEROMETER_WINDOW`. The cause was a fixed requirement for 80 samples inside 1.5 seconds. At 50 Hz, 80 samples span approximately 1.58 seconds, so the gate was impossible to satisfy.

Local VIO v0.2.2 now selects the newest 80 accelerometer and 80 gyroscope samples before the common IMU timestamp. The window is accepted when those samples span no more than five seconds. This preserves stale-history rejection while supporting 50 Hz and higher IMU configurations. Status now reports the selected window counts and inferred sample rates.

Hardware validation should confirm `80/80 @ approximately 50/50 Hz`, blocker `none`, Skill `SUCCEEDED`, VIO `TRACKING`, and increasing IMU propagation steps.

## Reason for the architecture change

The original design required gyroscope and accelerometer measurements to propagate body state continuously, with visual observations used to constrain drift. The first implemented Local VIO backend instead made RGB-D PnP the primary pose estimator and added IMU checks and gravity corrections around it. That visual-first architecture performed poorly during fast rotation and weak lighting.

## v0.3.8 behavior

- Every ordered IMU sample propagates a 15-state error-state filter.
- State includes orientation, position, velocity, gyroscope bias, accelerometer bias, and covariance.
- RGB plus aligned depth supplies the preferred metric visual correction.
- IR plus native depth is an optional synchronized low-light correction source.
- Raw ORB/PnP remains the baseline visual frontend; circular LCN remains an optional candidate.
- Visual measurements are gated by innovation size and Mahalanobis distance.
- High-rate predicted poses are published between camera updates without advancing a second committed filter state.
- Gravity leveling remains independent, quiet-IMU gated, roll/pitch only, and bounded.
- Existing startup, reset, session epoch, motion-inhibit, and point-cloud behavior is retained.

## Important classification

This backend follows the correct inertial propagation / visual update architecture, but it is still a reference implementation. Its visual measurement is a frame-to-frame RGB-D/IR pose correction. It is not yet a feature-level MSCKF or a nonlinear fixed-lag smoother.

## Focused hardware validation

1. Start with the Bolt stationary and confirm automatic initialization succeeds.
2. Confirm the Pose Propagation indicator updates at a higher rate than visual corrections.
3. Pivot quickly and verify orientation continues through weak or blurred camera frames.
4. Stop on a textured view and confirm RGB-D correction reduces accumulated drift without a large discontinuity.
5. Repeat in the dim room and observe whether `IR_DEPTH` becomes the selected correction source.
6. Confirm the gravity indicator retains the previously acceptable timing and correction rate.
7. Record status fields: visual sensor, visual age, correction magnitude, IMU propagation steps, filter covariance, and bias estimates.
8. Add recording/replay before tuning filter noises extensively.

## Next engineering milestone

Build a deterministic sensor recording and replay Provider, then evaluate the same trajectories through:

- The Python 15-state ESKF reference backend.
- A native Basalt adapter.
- An OpenVINS/MSCKF evaluation build, subject to GPL licensing constraints.

Use trajectory accuracy, orientation latency, visual-outage drift, reacquisition behavior, CPU load, and Windows deployment complexity to select the production backend.
