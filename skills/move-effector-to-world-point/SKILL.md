---
name: move-effector-to-world-point
description: Move the robot's controlled effector origin to a known absolute point in the active Midbrain world frame while preserving its measured orientation. Use for collision-checked free-space positioning when a tool result or operator provides world XYZ coordinates, including staging above a contact-work start point. Do not use for relative motion, intentional contact, force work, or raw joint control.
---

# Move Effector to World Point

Use the `move_effector_to_world_point` tool for one finite, collision-checked Integrated-controller move to an absolute world coordinate.

## Workflow

1. Take the absolute XYZ point directly from the operator or the structured result that identified it. Do not inspect runtime state or calculate a relative displacement.
2. Pass `target_position_world_m` in meters using Midbrain convention `MIDBRAIN_X_FORWARD_Y_LEFT_Z_UP_V2`.
3. When the source result provides a world-frame ID or VIO session epoch, copy them exactly into `target_world_frame_id` and `target_session_epoch`. Use `null` only when the operator intentionally specifies coordinates in the currently active world frame.
4. Use `execution_backend: IMPEDANCE` unless the task explicitly needs the Basic `POS_SPEED` (`POSITION_VELOCITY_LIMITED`) backend. Both backends receive a 50 Hz paced target stream, and the selected backend is signed into the exact preview. Pass `requested_speed_m_s` only when a nominal endpoint speed was requested; otherwise use `null` and let the controller choose its normal duration.
5. Treat the call as successful only when `physical_motion_completed` is `true`. A frame mismatch, epoch mismatch, unavailable transform, dependency failure, rejected preview, or authorization failure means no motion was completed.
6. In a compound workflow, do not begin a later contact action unless this move confirms completion or reports `ALREADY_AT_WORLD_POINT` with `physical_motion_completed=true`.

## Semantics and boundaries

- The target is the controlled-effector origin, not an anonymous flange or joint location.
- The host resolves the absolute world point into the configured arm-base frame with the current reviewed rigid transform.
- The Skill preserves the measured controlled-frame orientation and therefore requests `POSE_6DOF` IK.
- The host owns preview, exact call-scoped authorization, and signed commit. Never ask the model to copy or choose a preview ID.
- This Skill always ends in Integrated `FLOAT`, like other free-space positioning actions.
- This Skill does not authorize touching, cutting, pressing, scraping, gripping, or any other contact work.
