# Physical Agent Test Scaffold 0.2.9

The test agent runs Initialize Space Cognition automatically approximately one second after the GUI service starts unless `AUTO_INITIALIZE_SPACE_COGNITION=false`.

The browser UI exposes independent indicators for:

- Visual correction state and selected source (`RGBD`, `IR_DEPTH`, or no accepted update).
- Inertial pose propagation mode and IMU integration step count.
- Rotation estimator source.
- Gravity adjustment (`OFF`, `READY`, or `ACTIVE`).
- Feature extraction mode and low-light candidate selection.
- Map capture state.
- Initialization/reset state and session epoch.

The UI distinguishes pose propagation from visual correction. A pose may continue updating from IMU samples while the visual correction light is stale or unavailable. Visual correction diagnostics include accepted/rejected state, selected RGB-D or IR/depth source, reprojection error, correction magnitude, and time since the latest accepted visual update.

The gravity lamp retains the established behavior. Gravity changes roll and pitch only, preserves yaw and translation, and uses startup gyroscope bias/noise measurements to determine the effective quiet threshold.

The world RGB point cloud remains an orthographic isometric view. Orange marks world down and cyan shows the current camera frustum. Points are transformed into the current VIO world frame and fade over ten seconds.

Run `scripts/setup.ps1`, then `scripts/run.ps1`.
