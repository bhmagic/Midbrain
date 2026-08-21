# Changelog

## 0.1.32 - 2026-08-20

- Advance the Basic-owned grip-control profile to torque-only contact inference
  at an absolute measured `0.15` N m for 10 consecutive 50 Hz samples.
  Position and velocity remain published diagnostics but no longer decide
  contact.
- Raise the owner-requested attended-development all-active-joint new-grip
  admission gate from `70` to `85` C. Missing, stale, and non-finite
  temperature feedback remains ineligible.

## 0.1.31 - 2026-08-20

- Move the owner-observed normal-object gripper close target from `0` to `+12`
  degrees and set the distinct Basic hard/absolute close limit to `+17`
  degrees.
- Keep the gripper operational and calibration ceiling at the `+12` degree
  normal target. The other six joint limits and the `-20` degree gripper
  safe-home target are unchanged.
- Setup migrates only unchanged earlier `-20`, `-10`, or `0` degree gripper
  bounds and advances the affected immutable model, calibration, and assembly
  identities.
- Advance both mounted-effector compatibility revisions to v5 and the collision
  profile compatibility revision to v3 so every selected profile binds the new
  Basic model revision consistently.

## 0.1.30 - 2026-08-20

- Add the `midbrain.skill.locate_arm_base.v1` mounted-effector extension with a
  coarse visual landmark, eligible point names, VLM description, and
  controlled-frame offset for both qualified development effectors.
- Revise the bare-gripper and blade profile identities to v4. Locate Arm Base
  owns interpretation of its extension; Basic continues to own assembly
  selection, joint state, and timestamped FK and does not acquire visual
  workflow policy.

## 0.1.29 - 2026-08-20

- Raised the attended-development gripper operational velocity boundary from
  `2.1` to `4.0` rad/s. This owner-requested test value exceeds the official
  reBot application `vlim` of `3.0` rad/s but remains below the configured
  DM-J4310 motor envelope.
- Retained the `0.75` native FORCE_POS velocity translation and measured-speed
  brake, so a `4.0` rad/s position/effort request sends a `3.0` rad/s native
  motor ceiling.
- Setup migrates only the unchanged prior `2.1` rad/s model and calibration
  boundary and updates the immutable model, calibration, and assembly bindings.

## 0.1.28 - 2026-08-20

- Moved the owner-observed normal-object firm-grip endpoint and development
  gripper hard maximum from `-10`/`-9` degrees to `0` degrees. The `-20`
  degree safe-home target remains unchanged.
- Setup migrates only the prior unchanged `-20` or `-10` degree operational
  envelope and the prior `-9` degree hard maximum. Customized envelopes still
  require explicit review, and migrated model, calibration, and assembly
  identity bindings change together.

## 0.1.27 - 2026-08-20

- Extended the normal-object gripper operational and default-calibration close
  envelope from `-20` to `-10` degrees while retaining the observed `-9`
  degree hard boundary.
- Setup migrates only the unchanged prior gripper envelope, preserves the
  `-20` degree safe-home target, and changes the model/calibration revision
  bindings. Customized gripper envelopes require explicit review.

## 0.1.26 - 2026-08-20

- Expanded J4, J5, and J6 operational limits to the owner-observed `+/-85`,
  `+/-90`, and `+/-170` degree envelopes. Corresponding conservative hard
  bounds are `+/-95`, `+/-100`, and `+/-180` degrees.
- Setup now migrates both the active arm-model registry entry and the active
  calibration limits, changes their immutable revision bindings, and preserves
  all unrelated measured calibration values.

## 0.1.25 - 2026-08-19

- Added a generic per-actuator-group `POSITION_EFFORT_LIMITED` command-mode guard. The guard requires a complete matching command before activation and blocks incompatible commands, group float, and group lease release until explicitly cleared.
- Published physical joint temperature feedback from MotorBridge MOS/rotor samples so higher Providers can enforce conservative task gates without owning the motor transport.

## 0.1.24 - 2026-08-19

- Route the profiled full-arm no-effector reference to both arm-base seed
  localization and bounded orientation review so the Skill can distinguish
  the base joint from external support hardware.

## 0.1.23 - 2026-08-18

- Seed and preserve a Provider-local `config/arm_profiles` registry, migrate
  the ignored central assembly selection from the legacy mutable arm-model
  path, and serialize JSON as UTF-8 without a BOM for strict consumers.
- Allow Resource Provider Manager to present guarded physical-arm selection
  parallel to mounted-effector selection without moving profile ownership into
  Manager or a Skill.

## 0.1.22 - 2026-08-18

- Preserve the selected arm model's flexible `appendix` object and publish it
  in `robot_arm.assembly_state` so namespaced Skills can bind configuration to
  the active arm profile without importing the arm Provider.

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

- Make repeated-shutdown stationary confirmation wait for fresh feedback that
  spans the full configured observation duration. A bounded feedback-acquisition
  allowance replaces scheduler-sensitive cycle subtraction, so a stationary
  arm is accepted reliably without weakening the measured-rest requirement.
- Set the operational IMPEDANCE, POS_VEL, and POS_TOR velocity boundaries to
  4.0 rad/s for all seven joints. Publish all three mode-specific vectors under
  `command_limits`; retain the wider configured motor envelope as a separate
  hardware boundary. The J4-J6 and gripper developmental caps exceed the
  official reBot application `vlim` of 3.0 rad/s but remain below Basic's
  configured 10.0 rad/s motor envelope.
- Change the public `POSITION_EFFORT_LIMITED` torque ceiling from a motor ratio
  to `torque_limit_nm`, publish effective per-joint velocity and torque limits,
  and keep FORCE_POS ratio conversion private to the hardware adapter. The
  development UI now edits N·m and shows read-only Basic and motor ceilings.
- Treat an omitted control `resource_id` and the canonical root resource ID as
  the same root authority across acquire, renew, command, payload,
  gravity-float, and release requests. Declared child resources retain
  actuator-group routing, and unknown resources remain rejected.
- Retire the abandoned automatic friction-calibration workflow from the
  Hardware Development UI and Provider. Remove its motion, fit, persistence,
  session-replay, and range-search APIs while retaining attended pointer-
  deadman joint testing, measured-state display, local collision diagnostics,
  gravity float, and safe home.
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
