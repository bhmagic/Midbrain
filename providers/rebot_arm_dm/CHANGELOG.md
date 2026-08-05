# Changelog

This file records release-level outcomes. Historical entries may use the
device or Integrated aliases current at the time; use the
[command terminology map](README.md#command-terminology) for implementation
work.

## Unreleased

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
