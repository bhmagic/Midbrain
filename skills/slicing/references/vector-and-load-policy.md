# Slicing vector, profile, path, and load policy

## Profile ownership

The active Provider-owned mounted-effector profile owns blade-use directions
under `extensions.midbrain.skill.slicing_blade_profiles.v1`. Each numbered
profile contains `blade_direction_effector` and
`slicing_direction_effector`, plus an optional `locked_joint_names` array. It
describes one way to use the mounted blade, not one fixed physical blade
direction for every task. A lock holds the joint's measured position captured
at the first Contact move; it is not a separate position input.

The Slicing Skill owns numbered motion profiles in
`config/motion_profiles.json`, initialized from
`config_templates/motion_profiles.default.json`. Each profile owns blade load,
outward retract distance, engage delay, planned slice wait rate, and retract
delay. Each store declares its own existing default profile. Profile numbers
may contain gaps; a newly saved profile takes the lowest missing positive
number.

## Priority-preserving alignment

Normalize both blade directions. Project each slicing direction into the plane
perpendicular to its corresponding blade direction, then normalize it. Reject
a zero or parallel/antiparallel input whose projection is degenerate. This
keeps blade alignment exact and gives projected slicing alignment secondary
priority.

For each frame, derive the third direction as `blade cross slicing`. Form the
right-handed bases `[blade, slicing, third]`; compute world-from-effector as
`world_basis * transpose(effector_basis)`.

## Derived path

Let `B` be the normalized inward world blade direction, `S` the normalized
projected world slicing direction, `P0` the resolved begin point, `L` the slice
length, and `R` the profiled retract distance:

- engage point: `P0`
- slice endpoint: `P0 + S * L`
- planned retract endpoint for inspection: `P0 + S * L - B * R`
- executed slice displacement: `S * L`, resolved from the fresh measured
  controlled-effector position when Contact accepts the slice move
- executed retract displacement: `-B * R`, resolved from the fresh measured
  controlled-effector position when Contact accepts the retract move

Thus a tabletop cut with `B` pointing into the table requests extraction
outward even when the preceding slice endpoint was unreachable. The measured-
start slice also preserves the requested slice direction and length when the
absolute engage target was deliberately unreachable. Do not execute slice or
retract toward their planned absolute endpoints: doing so would mix the next
stroke with correction of the preceding position residual.

Use the profiled engage delay after the engage command. Use `L / V` after the
slice command, where `V` is `slice_wait_speed_m_s`. This is a Skill scheduling
minimum, not a commanded Cartesian speed. Contact executes each of the three
moves as a sequential-IK Cartesian segment at Basic's advertised internal
control rate and derives segment duration from Basic's joint limits. For every
move, use the larger of the profiled minimum and Contact's velocity-limited
transition time with the shared runtime margin. Use the profiled retract delay
under the same rule before relaxation. Reject a profile-derived delay longer
than 55 seconds so the next-command timeout remains bounded. The Provider
starts the safe inactivity interval after its calculated transition time. If
the runtime hold reaches that signed transition-plus-watchdog window, fail and
relax instead of overrunning the authorized interval.

Inspect Contact's per-move tracking observations after a trial. Commanded
cross-track error describes the joint-interpolated kinematic path; measured
cross-track and joint tracking errors describe physical following reconstructed
from Basic feedback. They diagnose straightness but do not establish whether a
cut succeeded.

## World-to-arm conversion

Use exactly one Manager activation with `state=ACTIVE`, `motion_usable=true`,
and no expiry. Let `world_from_base = (R_wb, t_wb)`. Convert world points with
`p_base = transpose(R_wb) * (p_world - t_wb)` and orientation with
`R_base_effector = transpose(R_wb) * R_world_effector`.

## Joint-lock and pose policy

Integrated performs the full requested orientation before the Contact
handoff. At the first Contact move, Contact captures every profiled locked
joint from measured state and keeps those positions as hard constraints across
engage, slice, and retract. It then minimizes one weighted full-pose residual
over the unlocked joints, retaining the best iterate instead of assuming that
all six Cartesian components remain exactly reachable after degrees of
freedom are removed. Position and orientation residuals are reported as
command telemetry; they are not task-success claims.

Bind both stages to the activation. After Integrated reaches verified FLOAT,
capture its actual measured controlled-frame pose and completed plan identity.
Allow up to `0.35 rad` geodesic orientation drift from that actual handoff,
not from the nominal IK target. Reject a changed controller plan, active
trajectory, unconfirmed FLOAT, or changed workcell calibration before Contact.

After those identity and drift checks, transition Integrated from `HOT` to
`WARM` through Manager. Integrated's `WARM` transition must finish gravity
float and release its fenced Basic lease synchronously. Re-read Integrated's
observation and require `residency=WARM`, `float_confirmed=true`, no active
trajectory, and `lease.active=false` before activating Contact. A failure at
this boundary is a preflight rejection: no Contact session was submitted.

## Force-capacity vector

Let `F = blade_load_kgf * 9.80665`. In the orthonormal world slicing basis,
construct `F_world = F * blade + 0.5 F * slicing + 0.5 F * third`. Convert it
with `F_base = transpose(R_wb) * F_world`.

The Contact Provider uses this vector to derive gravity-aware joint effort
ceilings; Basic does not receive a Cartesian force-controller command. All
current Slicing plans set rotational wrench to zero. The Provider supports
rotational wrench mapping, but no current Slicing qualification uses it.
