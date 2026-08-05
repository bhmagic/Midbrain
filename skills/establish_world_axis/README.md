# Establish Current World Axis

This finite Skill exposes the non-reset readiness path of
`initialize_space_cognition`. It starts the configured camera and Local VIO as
needed, verifies that status and body pose agree on one current convention-V2
epoch, and temporarily acquires global motion inhibit when Local VIO still
needs stationary IMU samples.

It does not locate the arm base, reset the VIO origin, revoke a reviewed
world-to-arm calibration, or submit robot motion. Spatial adapters use the same
operation as a prerequisite so an independently invoked item or effector
locator can recover from a cold or uninitialized VIO provider.
