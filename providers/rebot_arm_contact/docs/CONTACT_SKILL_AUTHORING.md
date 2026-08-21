# Contact Work Skill authoring

A Contact Work Skill is finite and task-specific. It creates one complete
plan, acquires the Manager advisory arm-group lease, embeds that exact lineage,
signs the resulting plan with its installed identity, sends each planned
sequence number at its monotonic deadline, and sends `RELAX` from terminal
cleanup even after an intermediate or ambiguous transport failure.

Every pose is expressed in the Basic arm root frame. Every wrench declares
either that root frame or the selected acting frame. The force convention is
tool-on-environment at the acting point.

Use `ABSOLUTE_ROOT` when `position_m` is a root-frame endpoint. Use
`RELATIVE_TO_MEASURED_EFFECTOR_ROOT_AXES` when it is a signed root-axis
displacement that must begin from the fresh measured controlled-effector
position at move acceptance. Relative mode is appropriate for bounded
extraction after a deliberately unreachable contact target; it prevents prior
position residual from changing the extraction direction.

The canonical wrench order is `(fx, fy, fz, tx, ty, tz)`. The Provider maps all
six fields through the complete geometric Jacobian transpose and adds their
joint contribution to the gravity-aware effort ceiling. The first Skill in
this repository, `slicing`, has only been prepared for force components: it
must send `tx = ty = tz = 0`. That is a qualification statement for Skill authors, not a
Provider validation gate or programmed warning. A future Skill may use
rotational components after that Skill is separately tested.

The Provider does not wait for arrival. Each step selects `ONE_SHOT` or
`CARTESIAN_SEGMENT`. One-shot commands latch the solved endpoint directly. A
Cartesian segment is one replaceable Skill move whose internal sequential IK
knots are streamed at Basic's advertised internal control rate. Its requested
orientation is held at every knot, and hard locks apply to every solve. The
first endpoint establishes Basic's arm-group position/effort mode guard. Later
moves retain that guard and build their command trajectory from the previous
commanded endpoint rather than resetting to lagging measured joints.

`delay_after_accept_s` is the signed physical-stage dwell. The shared runtime
waits for Contact's matching `trajectory_complete` observation, then services
authority through the complete dwell before submitting the next step. Contact
itself still has no Provider-side arrival queue or task-success gate; another
valid caller may replace the endpoint immediately. Each step's
`next_command_timeout_s` is a signed safe inactivity interval after Contact's
Basic-limit-derived transition time and must leave room for trajectory
completion and the dwell. Contact freezes the deadline at acceptance; it does
not extend it from measured arrival. If the next accepted step does not arrive
in that transition-plus-watchdog window, the Provider fences the session and,
when no carry is confirmed, returns to Basic gravity float.

Skills do not specify joint velocity limits. An accepted move returns the
Basic-declared `joint_velocity_limits_rad_s` used for the move and
`velocity_limited_transition_time_s`. For one-shot moves this is derived from
fresh measured joints and the solved endpoint. For Cartesian segments it is
the larger of sequential-IK joint-limit timing and Cartesian distance under
Contact's configured 0.1 m/s ceiling. The transition time does not consume
`next_command_timeout_s`; the runtime still verifies that trajectory waiting
plus the complete dwell fits the signed transition-plus-watchdog window. This
prevents a short nominal dwell from truncating a stroke while retaining the
rule that Contact never asserts Cartesian arrival or task success.

The runtime result includes `move_tracking_observations`. Each observation is
a diagnostic FK reconstruction from Basic's measured joints after trajectory
completion and the planned dwell. Compare commanded cross-track error with
measured cross-track and joint tracking errors when tuning a path. Do not
interpret those values as material-contact success or an arrival gate.

Each installed Skill has a separate configured signing credential. A Skill
process receives only its own credential; the Provider receives the matching
verification credentials for its allowlist.

Do not use a generic Agent-facing Skill that exposes arbitrary coordinates,
wrenches, lock masks, or effort values. A discoverable Skill should constrain
those values to its named need, derive them from its own deterministic policy
and validated evidence, and sign only the resulting bounded plan. VLM output
may provide task evidence; it must not receive the signing secret.

Locked joints use the full Basic-authorized torque ceiling in N·m and retain their first measured
session position. They are hard IK constraints. The solver searches the
unlocked joints for the lowest weighted full-pose residual, retains its best
iterate, and reports separate position and orientation residuals when the
requested 6-DoF pose is incompatible with the locks. Do not use a lock mask as
a clamping substitute. The initial `slicing` Skill is explicitly non-clamping;
its mounted-effector blade-use profile may request locks needed by that use
orientation.

## Slicing boundary

`slicing` is the only initial task Skill. A numbered profile on the active
mounted effector supplies its blade-use and slicing directions. The invocation
supplies one begin point, an inward world blade direction, a preferred world
slicing direction, and a slice length. Blade alignment is exact; each slicing
direction is projected into its plane perpendicular to the blade direction.
The Skill derives the slice endpoint and a planned retract endpoint for
inspection. The executed retract is instead the same signed negative-blade
displacement resolved from measured effector position at move acceptance.

The Skill first delegates a rotation-only collision-checked alignment to the
existing Integrated free-space Skill. Integrated must finish its accepted target
in verified `FLOAT`, and the active workcell transform must remain unchanged.
The host then places Integrated in `WARM` and verifies from Integrated's own
observation that its trajectory is inactive and its Basic lease is released.
Only then does `slicing` activate Contact and submit engage, slice, and retract
as three consecutive `CARTESIAN_SEGMENT` moves. Engage is absolute. Slice and
retract are root-axis displacements resolved from fresh measured effector
positions when their respective moves are accepted. This prevents engagement
or cutting residual from changing the requested slice length, slice direction,
or extraction direction. Each is a close sequential-IK Cartesian approximation
rather than an arrival or exact-path guarantee.
Contact performs no collision planning.
Terminal cleanup requests `RELAX`.

The selected Skill-owned motion profile supplies one positive
`blade_load_kgf`. The Skill converts it with
`1 kgf = 9.80665 N`, uses that component along the blade direction and half
that component along each of the projected slicing and derived third axes,
then converts the vector into the Basic root frame. All three moves use the
same force-capacity vector.

Blade-use directions belong to the Slicing-owned namespaced extension of the
mounted-effector profile. Multiple numbered use profiles may describe one
physical blade. A blade-use profile also owns any hard joint locks for that use.
Load, outward retract distance, and timing belong to a separate numbered motion
profile in the Slicing Skill. Omitted selections use each store's declared
default profile rather than assuming profile `#1` exists.
