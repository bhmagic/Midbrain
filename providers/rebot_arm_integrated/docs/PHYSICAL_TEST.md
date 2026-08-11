# Attended physical qualification

Read [Integrated safety](SAFETY.md) and
[Basic safety](../../rebot_arm_dm/docs/SAFETY.md) first. This procedure is for
attended hardware qualification of the autonomous signed free-space path. It
does not restore retired manual staging or teleoperation interfaces.

## Prerequisites

- Manager and Fabric are healthy.
- Basic is healthy in verified gravity float.
- Integrated is HOT with its fenced arm-group lease.
- No global motion inhibit is active.
- The Basic-published assembly ID, fingerprint, mounted-effector revision,
  inertial values, and collision profiles match the installed hardware.
- No undeclared object is held.
- The emergency gravity-float and authoritative safe-home paths are available.

Confirm `GET /v1/capabilities` advertises only the current free-space surface.
The developer page must not expose target editing, engagement, gamepad,
gripper, contact, or runtime-settings controls.

## Bounded test sequence

1. From the normal Agent UI, request a 5 mm position-only free-space move in a
   direction with generous verified clearance.
2. Confirm the operation uses `perform_relative_effector_motion`, creates a
   current signed path plan, executes without a human approval prompt, reaches
   a measured terminal state, and returns to the requested safe final state.
3. Repeat with a small arbitrary three-axis displacement. Confirm it remains
   one combined Cartesian goal and never becomes three sequential axis moves.
4. Repeat with a small orientation-only goal, then a combined translation and
   orientation goal. Verify position and orientation residuals separately.
5. Place a work object beyond the requested destination. Confirm motion is
   allowed up to the zero-extra-margin non-contact boundary.
6. Place a `KEEP_OUT` obstacle in the direct route. Confirm 10 mm extra
   clearance and either rejection at the start or `CLOSEST_SAFE`; the arm must
   not rise or move laterally to invent a detour.
7. Confirm all selected mounted-effector spheres appear in the main 3D viewer
   and the control audit names the same profile revision.

Stop immediately on unexpected descent, path shape, collision classification,
profile mismatch, loss of gravity support, transport error, or uncertain
physical outcome. Do not automatically retry.

## Current development observation

On 2026-08-11, the operator reported successful execution of several 6-DoF
free-space motions during development of commit `f246ae7` with the selected
`rebot_b601_dm.5_inch_blade` assembly. After final tuning, the checked-in
development profile records 0.33 kg total effector mass and COM
`[-0.165, 0.0, -0.03]` m in `end_link`. Its four temporary collision spheres
remain owned by that profile in `rebot_arm_tool`.

This is a development observation, not a guarded-physical qualification
record. The report did not preserve the exact requested poses, log identifiers,
authority interval, acceptance measurements, or terminal safe-state evidence
required by the repository validation contract. It demonstrates that the
arbitrary 6-DoF path was exercised successfully, but it does not qualify every
pose, obstacle arrangement, clearance boundary, failure path, or the temporary
collision envelope. Keep the profile and controller qualification at
`DEVELOPMENT` until a bounded run records all required evidence.

## Excluded tests

Do not use this Provider to test gripping, cutting, pushing, pressing,
scraping, contact-force behavior, manual target staging, or motion with an
undeclared held object. These require separate qualified controllers.

## Safe termination

The authoritative shutdown command from the repository root is
`./providers/rebot_arm_integrated/scripts/stop_physical_gui_test.ps1 -ProjectRoot (Resolve-Path .) -StopCore`.

GUI launch acknowledgement is not evidence that safe-home completed. Inspect
the safe-termination log and physical arm state.
