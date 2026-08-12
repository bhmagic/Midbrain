# Changelog

## 0.1.21 - 2026-08-07

- Requires a fresh feedback generation from every motor after each batch request;
  cached MotorBridge state can no longer satisfy a later control cycle.
- Allows 40 ms for that fresh seven-motor batch so normal approximately 16 ms
  acquisition retains bounded Windows scheduling margin under concurrent vision
  load.
- Makes Manager `HOT` an explicit fault-recovery transition: Basic requires
  recent generation-verified feedback, fences prior control authority, and
  restores gravity float rather than remaining permanently faulted after a
  transient feedback miss.
- Publishes joint state and local FK at the measured feedback-acquisition estimate
  with per-joint generation, timestamp uncertainty, and acquisition telemetry.
- Separates Manager heartbeat, motion-inhibit polling, joint publication, and FK
  publication so a slow control-plane request cannot stall transform output.

This file records release-level outcomes. Historical entries may use the
device or Integrated aliases current at the time; use the
[command terminology map](README.md#command-terminology) for implementation
work.

## Unreleased

- Revise the five-inch-blade VLM landmark description to identify the adjoining
  blade as metallic while retaining the military-green handle as the only
  alignment surface. Blade reflectance, finish, and apparent subtype cannot by
  themselves make the scene unsuitable.
- Move VLM arm-root translation alignment data from a Skill-private gripper
  profile into optional namespaced extensions of the Provider-owned mounted-
  effector profiles. The aligner now follows Basic's active assembly selection,
  requires every configured landmark point before taking their 3D arithmetic
  mean, and supports independently configurable VLM descriptions and rigid
  controlled-frame offsets. The five-inch blade begins with a two-endpoint
  military-green handle landmark and an unverified
  `[-0.090, +0.010, -0.070]` m trial offset. Reference-image resolution remains
  explicitly future work.
- Record the final checked-in development values for the `5 inch blade`
  mounted-effector profile: 0.33 kg total effector mass, center of mass
  `[-0.165, 0.0, -0.03]` m in `end_link`, and the four operator-tuned temporary
  collision spheres in `rebot_arm_tool`. Regression tests now bind those exact
  values so later physical changes require an explicit profile revision.
- Let fixed-tool profiles mark replaced effector joints inactive. Inactive
  motors are excluded from resource leases, MotorBridge registration, feedback
  freshness, mode transitions, and commands while retaining an explicit
  `INACTIVE_NOT_INSTALLED` legacy state slot.
- Recompute terminal-link standard-gravity weight from the selected effector
  mass instead of retaining the previous effector's derived value.
- Updated the requested installed-arm POS_SPEED/`POS_VEL` cap vector to
  5 rad/s for J1–J3 and 10 rad/s for J4–J6 and the gripper. These are command
  caps inside the documented motor envelope, not continuous-duty whole-arm
  qualification; conservative MIT and calibration limits remain unchanged.

## 0.1.20 - 2026-07-29

- Added fenced payload mass/tool-COM configuration and included payload gravity
  torque in impedance, gravity-float, and safe-home support with motor-limit
  clipping.
- Added dedicated attended-test endpoint limits, unchanged-endpoint keepalive,
  staged motor-mode transitions, early supporting MIT frames, and bounded
  retries for selected Windows serial and mode-confirmation failures.
- Added gripper effort-limited speed-ceiling translation and measured-feedback
  brake/hold telemetry.
- Preserved powered support across Basic/Integrated lease handover and made
  safe-home revoke the operational writer before its first supported command.
- Kept graceful-stop timeout from automatically force-killing the Basic
  process when powered support may still be active.
- Physically exercised attended gripper motion, lease transfer, bounded
  Cartesian motion, gravity support, and final safe-home in the guarded
  workcell; newer caps and contact/payload behaviors remain separately subject
  to validation.

Earlier detailed development history remains available in Git.
