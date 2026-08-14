---
name: slicing
description: Plan and execute one finite non-clamping blade slice from a begin point, inward world blade direction, projected world slicing direction, and slice length. Use numbered mounted-effector blade-use profiles and Slicing motion profiles, align through Integrated, then submit engage, slice, and outward-retract Contact Cartesian segments followed by relaxation.
---

# Slicing

Execute only one finite non-clamping slicing workflow. Do not reinterpret this
Skill as a generic contact path, clamp, grip, or unbounded sawing cycle.

## Agent inputs

Require:

- `slice_begin_point_m`: the first cutting point.
- `blade_direction_world`: the inward blade direction, such as into a table.
- `slicing_direction_world`: the preferred world slicing direction.
- `slice_length_m`: the positive finite slice length.

Accept `point_mode=ABSOLUTE_WORLD` or
`RELATIVE_TO_CURRENT_EFFECTOR_WORLD`. In relative mode, interpret only the
begin point as a world-axis offset from the current measured controlled
effector. The two directions always remain world-axis vectors.

Accept nullable `blade_profile_number` and `motion_profile_number`. Require the
Agent to send `null` unless the user explicitly requests a profile number.
Resolve `null` from the corresponding store's live `default_profile_number`
when execution begins. Do not ask the Agent to reproduce effector vectors,
joint locks, load, retract distance, or timing values already owned by
profiles.

`integrated_execution_backend` selects only the Stage 1 alignment backend.
It defaults to `IMPEDANCE`; `POS_SPEED` maps to Basic
`POSITION_VELOCITY_LIMITED`. Both use Integrated's signed 50 Hz paced path.
This option never changes Stage 2, which remains Contact-owned `POS_TOR`.

## Workflow

1. Read the selected blade-use profile from the active mounted-effector
   profile's `extensions.midbrain.skill.slicing_blade_profiles.v1` object.
2. Read the selected load/retract/timing profile from the Slicing Skill.
3. Normalize the inward blade direction. Project `slicing_direction_world`
   into the plane perpendicular to it and normalize the projection.
4. Derive the planned world geometry:
   - engage = resolved begin point;
   - slice = engage + projected slicing direction × slice length;
   - planned retract endpoint for inspection = slice − inward blade direction × profiled retract distance;
   - executed slice = the same projected slicing displacement resolved from
     the fresh measured effector position when Contact accepts move 2;
   - executed retract = the same signed negative-blade displacement resolved
     from the fresh measured effector position when Contact accepts move 3.
5. Project the effector slicing direction perpendicular to the profiled
   effector blade direction. Compute the right-handed orientation that aligns
   blade direction exactly and projected slicing direction secondarily.
6. Ask Integrated to execute one rotation-only `SET_ARM_BASE_RPY` move and
   finish in verified `FLOAT`. Capture the actual measured FLOAT handoff pose
   and completed Integrated plan identity.
7. Before Contact, require unchanged workcell calibration, the same completed
   Integrated plan, an inactive Integrated trajectory, verified FLOAT, and no
   more than `0.35 rad` actual orientation drift from the captured handoff.
8. Ask Manager to place Integrated in `WARM`. Confirm from Integrated's own
   observation that it is `WARM`, remains in verified `FLOAT`, has no active
   trajectory, and no longer owns a Basic lease.
9. Only after that proof, activate the independent Contact Work Provider.
   Submit engage as an absolute `CARTESIAN_SEGMENT`. Submit slice and retract as
   `CARTESIAN_SEGMENT` displacements in Basic-root axes, each resolved from the
   fresh measured effector position at acceptance. Use the blade profile's
   joint locks, then request `RELAX` in terminal cleanup. Contact captures each
   locked joint's measured position at the first Stage 2 move, treats it as a
   hard constraint for all three moves, and solves the closest weighted 6-DoF
   pose available from the remaining joints.

Use the motion profile's engage delay for the first command. Compute the slice
delay as `slice_length_m / slice_wait_speed_m_s`. These are signed minimum
delays. After each move is accepted, wait for the greater of the profiled delay
and Contact's measured-to-target joint transition lower bound, including the
shared runtime margin. Contact internally advances each segment through
sequential IK knots at Basic's advertised control cadence and reports its
Basic-limit-derived duration. Treat the profile rate only as planned command
spacing; it does not command Cartesian velocity or provide an arrival
assertion.
Use the same timing-floor rule for the profiled retract delay before relaxation.

Use the profiled blade load as the force-capacity component along the inward
blade direction. Apply half that component along the projected slicing axis
and half along the derived third axis. Convert with `1 kgf = 9.80665 N`,
transform into the Basic arm-root frame, reuse the force vector for all three
moves, and emit zero rotational wrench.

Do not claim that a point or cut was reached. Contact results describe command
handling only, and Contact has no collision planning. Read
[references/vector-and-load-policy.md](references/vector-and-load-policy.md)
before changing profile, path, alignment, force, or timing behavior.

## Developer staging

Use the numeric developer surface to type every value, prepare and inspect one
frozen plan, execute Integrated alignment as Stage 1, then execute Contact and
relax as Stage 2.

Selecting a profile fills the numeric fields; edited numbers remain usable for
the current developer plan. Saving blade-use vectors appends a numbered
profile, including its selected hard joint locks, to the mounted-effector
source profile. Each new Agent invocation verifies that the source still has
the active effector identity, then live-loads only this Skill-owned extension;
saves, deletes, and blade-default changes therefore require no workspace
restart. Basic's broader assembly-state copy remains unchanged until its normal
republication. Saving a motion profile writes the Skill's local
`config/motion_profiles.json` and is also available to the next invocation.
Either store may delete profile `#1`; saving reuses the lowest missing positive
number, and the developer surface can set any existing profile as that store's
default. A prepared developer plan remains frozen and does not adopt later
profile edits.
