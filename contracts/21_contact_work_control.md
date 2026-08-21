# Contact work control contract

## 1. Status and scope

This is the version 0.1 working contract for deliberate arm contact. It
defines the boundary among a finite Contact Work Skill, an independent Contact
Work Provider, the Basic hardware Provider, the Manager, and the World State
Fabric.

The Contact Work Provider is not an Integrated Controller mode. It has an
independent process, environment, lifecycle, control lease, command API, and
failure boundary. It communicates directly with the Basic Provider.

General collision planning is outside this contract. Basic joint limits,
fencing, deadlines, inactive-joint policy, and safe relinquishment remain in
force. A task-specific Skill owns the semantic decision that deliberate
contact with its workpiece is appropriate.

## 2. Separation of duty

The Skill:

- is finite and specific to one class of contact task;
- plans an ordered set of Cartesian poses, applied wrenches, lock masks, and
  delays, but does not select hardware joint-speed limits;
- signs the exact bounded plan using its installed Skill identity;
- submits one planned move at a time; the shared runtime waits for Contact's
  trajectory-complete observation and then applies the signed stage dwell
  before submitting the next move, without treating either as task success;
  and
- submits `RELAX` in its terminal cleanup path unless a confirmed carry is
  deliberately transferred to the persistent Contact and Grip Providers.

The Contact Work Provider:

- validates Skill authorization, assembly identity, frames, and numeric input;
- owns frame resolution within its declared frame support, constrained IK,
  Jacobian evaluation, gravity-aware effort ceilings, Basic-limit discovery,
  and command replacement;
- owns execution of either a direct one-shot endpoint or one active Cartesian
  segment, and holds its final endpoint until replacement, explicit relaxation,
  fault, lease loss, or inactivity timeout;
- commands Basic only through `POSITION_EFFORT_LIMITED`;
- publishes measured joint state and command disposition without deciding
  whether the task's Cartesian objective succeeded; and
- never imports or calls the Integrated Controller.

The Basic Provider remains the final authority for hardware transport, hard
and operational limits, motor-mode translation, local lease fencing, command
deadlines, gravity float, and safe relinquishment.

## 3. Plan and authorization

The canonical plan schema is
`contracts/schemas/contact_work_plan.v1.schema.json`. The canonical
authorization schema is
`contracts/schemas/contact_work_authorization.v1.schema.json`.

An authorization is short-lived and binds at least:

- installed `skill_id` and one `execution_id`;
- exact canonical plan SHA-256;
- Contact Work Provider ID, instance ID, and boot ID;
- Basic assembly fingerprint and mounted-effector revision;
- issue and expiry times; and
- an unpredictable assertion ID and nonce.

The Provider verifies the assertion with the configured credential for that
installed Skill. Model output, Agent text, and VLM output do not receive that
credential. A valid signature never bypasses Basic fencing or the Provider's
local validation.

During the Manager advisory-authority migration, a Skill acquires a Manager
authority lease for the arm group before it signs the plan. The signed plan
binds the Manager resource, owner, lease ID, permissions, and fencing
generation. The Provider publishes a versioned shadow comparison between that
upstream lineage, the live Manager record, and its authoritative Basic lease.
The Manager record remains coordination evidence in this phase; it does not
replace the signed plan or Basic's authoritative local lease, and numeric
Manager and Basic fencing generations are never compared.

## 4. Cartesian target and wrench

Each move carries one pose and one wrench. A pose contains a position value in
metres, a `position_mode`, and an `xyzw` quaternion. `ABSOLUTE_ROOT` interprets
the position in the Basic root frame. `RELATIVE_TO_MEASURED_EFFECTOR_ROOT_AXES`
interprets it as a root-axis displacement from the fresh measured controlled
effector position captured when that move is accepted. This relative mode is
signed plan content; it does not allow the Provider to invent a direction or
distance. A wrench contains:

- force `(fx, fy, fz)` in newtons; and
- torque `(tx, ty, tz)` in newton-metres.

The wrench frame and application point are explicit. The application point is
the selected mounted-effector controlled frame. The sign convention is the
requested wrench exerted by the tool on the environment. Joint effort ceilings
use absolute component demand, so the environment reaction sign does not
reduce the ceiling.

The Provider implements the complete mapping
`tau_wrench = J(q, acting_point)^T * wrench`. The first Contact Work Skill,
`slicing`, is qualified only with `tx = ty = tz = 0`. This is Skill-author guidance, not a
Provider gate: the Provider accepts and maps non-zero rotational components,
and does not emit a special warning for them.

## 5. IK and unreachable targets

The first endpoint begins from fresh measured joint positions. A chained
endpoint resolves any measured-start-relative target from fresh measured FK,
but its command trajectory begins from the previous commanded joint setpoint.
This distinction preserves Basic setpoint continuity when the physical arm is
still slightly behind the preceding endpoint; a new segment must not replace a
position/effort hold with the lagging measured joints. Explicitly locked joints
are hard constraints: they are removed from the solve and retain their first
captured session position. A joint that is unlocked and later locked again in
the same session reuses its first captured lock position.

The solver minimizes a weighted full-pose objective containing all three
position and all three orientation residual components over the remaining
unlocked joints. It accepts only improving numerical steps and returns the best
finite configuration encountered inside operational limits. Position and
orientation residuals are reported separately. Failure to meet a Cartesian
residual tolerance is not by itself a command failure because locks remove
degrees of freedom and contact trajectories may deliberately contain
unreachable endpoints. Invalid frames, non-finite results, stale feedback,
invalid assembly identity, or absence of a six-joint arm group remain hard
rejections.

The Contact plan does not contain joint velocity limits. For every accepted
move, the Provider uses the per-joint `POSITION_EFFORT_LIMITED` velocity limits
declared by the active Basic Provider. A `ONE_SHOT` move derives
`velocity_limited_transition_time_s` as the largest absolute
measured-to-target joint displacement divided by that Basic limit. A
`CARTESIAN_SEGMENT` divides the command-start-to-target translation into
sequential IK knots no farther apart than the configured Cartesian spacing and
time-parameterizes every joint interval against the same Basic limits. It also
enforces the Contact-owned maximum Cartesian command speed, currently `0.1`
m/s; the segment duration is the larger of joint-limit timing and Cartesian
distance divided by that speed. The resulting duration is still a command
timing lower bound, not an arrival assertion.
The frozen Basic velocity vector and derived time are returned with the move
disposition and published with the active move.

A Cartesian segment keeps the move's requested orientation at every IK knot.
This is appropriate for Slicing because Integrated aligns the tool before the
Contact handoff. The Provider linearly interpolates joint commands between
adjacent knots, so the path is a close sequential-IK approximation to a
Cartesian line, not a mathematical guarantee that the physical controlled
point follows an exact line. Residual telemetry exposes compromises caused by
hard locks, reachability, and numerical IK.

## 6. Joint effort ceilings

For every non-locked joint `i`, the initial policy is:

`wrench_i = abs((J(q)^T * wrench)_i)`

`limit_i = abs(gravity_i) + max(wrench_i, 0.20 * configured_tmax_i)`

`command_limit_i_nm = min(limit_i, Basic_torque_cap_i_nm)`

Gravity and the requested contact wrench are deliberately additive even when
their signed directions oppose. This conservative rule prevents cancellation
from reducing available load support. A capped result is published as
saturation; it is not silently described as an achieved Cartesian wrench.

Every explicitly locked joint uses the full Basic-authorized torque ceiling in
newton-metres. Lock acquisition captures one fresh measured position before
the endpoint is commanded. This is an intentional high-authority hold, not a
low-effort test.

Basic `POSITION_EFFORT_LIMITED` accepts position, velocity limit in radians per
second, and `torque_limit_nm`. Basic alone converts the SI torque ceiling to
the active motor adapter's vendor-specific FORCE_POS ratio. The gravity term
above therefore reserves effort capacity; it is not a separately commanded
gravity feed-forward torque.

While a move is active, the Provider recalculates Jacobian and gravity terms
from fresh measured joint positions. A one-shot IK endpoint remains latched.
For a Cartesian segment, the active joint command advances along the prepared
sequential-IK path and the final endpoint latches after the segment completes.

## 7. Replacement, timeout, and relaxation

Accepted move sequence numbers are strictly increasing. At the Provider API,
the next valid move replaces the active endpoint or segment regardless of
current progress or arrival. There is no Provider-side queue or task-success
gate. Replacement preserves the previous commanded setpoint as the next
segment's command origin, so measured tracking lag cannot create a momentary
setpoint collapse. The shared finite-Skill runtime additionally waits for
trajectory completion and the signed dwell before it submits the next move. A
Cartesian segment's internal IK knots are ephemeral execution detail for that
one active move; they do not weaken replacement semantics.

Contact reads Basic's advertised internal control rate and advances a Cartesian
segment at that cadence. The current Basic rate is 50 Hz. Each tick submits a
changing `POSITION_EFFORT_LIMITED` joint target to Basic; once the final target
is held unchanged, Basic may apply its normal duplicate-command suppression and
motor keepalive behavior. Contact has no independently configurable command
rate that can drift from Basic.

Each move supplies `next_command_timeout_s`. The default is six seconds after
the Provider's Basic-limit-derived transition interval. At acceptance, Contact
freezes the deadline as acceptance time plus
`velocity_limited_transition_time_s` plus `next_command_timeout_s`. This keeps
the motion's own calculated duration from consuming the safe inactivity wait.
Only acceptance of a new valid setpoint resets that deadline. Invalid, stale,
replayed, or duplicated content does not extend it.

`delay_after_accept_s` is the Skill's signed physical stage dwell. The shared
Contact runtime polls the matching endpoint until `trajectory_complete=true`,
then starts the complete dwell while continuing to service Manager authority.
It submits the next move only after that dwell. If trajectory completion plus
the dwell would reach the signed transition-plus-watchdog deadline, the
runtime fails and requests relaxation rather than knowingly overrunning the
authorized inactivity window. This runtime sequencing does not convert
trajectory completion or elapsed dwell into Cartesian arrival or task-success
evidence, and it does not add a Provider-side replacement gate for independent
callers.

On timeout, fault, lease loss, shutdown, or explicit `RELAX`, the Provider:

1. fences the active session against further setpoints;
2. requests Basic gravity float;
3. verifies that all active arm joints completed the MIT transition when
   feedback is available; and
4. releases its Basic lease.

A command racing with a completed timeout belongs to a new session and needs a
new authorization.

## 8. Observation and result semantics

The Provider publishes `robot_arm.contact_work.state` using
`contracts/schemas/contact_work_state.v1.schema.json`. At minimum it includes
fresh measured joint positions, velocities, torques, the active target and
lock mask, Basic velocity limits, effective torque ceilings in newton-metres,
wrench/gravity contributions, saturation,
session identity, command sequence, deadline, assembly fingerprint, and local
lease lineage.

The state also publishes a read-only `measured_acting_frame_pose` derived from
the same independent FK and fresh six-joint feedback. It contains the Contact
root frame, acting-frame identity, position, quaternion, and observation time.
Finite Skills may use it to preserve the current measured orientation for a
zero-displacement Contact acquisition; it is observation, not a new authority
or command path.

For each active Cartesian segment, Contact also derives command and measured
cross-track error, orientation error, joint tracking error, and measured
along-track fraction from Basic's measured joints and the same independent FK
model. The shared Skill runtime samples this record after each planned hold.
These fields diagnose commanded geometry versus physical following; they are
not arrival assertions or task-success evidence.

Disposition values such as `ACCEPTED`, `SUPERSEDED`, `REJECTED`,
`WATCHDOG_RELAXED`, and `EXPLICITLY_RELAXED` describe control handling only.
They do not assert that a Cartesian target or task objective was reached.

## 9. Initial qualification boundary

The initial implementation and `slicing` Skill are development software for a
non-clamping task. They receive software tests before physical qualification.
Physical testing must not introduce a low-torque load-bearing state. The first
physical boundary uses the selected blade development-v3 assembly and is
revised as tool geometry and contact behavior are measured.

Basic now publishes physical MOS/rotor temperature samples when the transport
provides them. Contact republishes arm feedback but does not own task thermal
policy. The independent Grip Provider applies the conservative whole-arm
new-grip gate defined by the Grip and Carry contract.

## 10. Initial slicing composition

`slicing` composes, but does not merge, the free-space and contact duties. It
uses the existing Integrated signed preview/commit path for one rotation-only
blade alignment with final state `FLOAT`. It then revalidates the same active,
motion-usable world-to-arm transform and the captured Integrated handoff.
Manager then moves Integrated to `WARM`; that transition must synchronously
confirm gravity float and release Integrated's Basic lease. The Skill verifies
Integrated reports `WARM`, confirmed float, no active trajectory, and no Basic
lease before Manager activates Contact. Integrated and Contact never own the
Basic lease simultaneously. Failure to prove relinquishment is a preflight
rejection and must occur before Contact session submission.

The alignment uses two directions in each frame. A numbered blade-use profile
in the active mounted-effector profile supplies the effector blade and slicing
directions. Each invocation supplies the inward world blade direction and a
preferred world slicing direction. In each frame the slicing direction is
projected perpendicular to the blade direction. The rotation maps the blade
direction exactly and maps only that projected slicing direction, then derives
a right-handed third axis.

The invocation supplies one begin point and slice length. The Skill projects
the world slicing direction, derives the slice endpoint from the requested
length, and derives the planned retract endpoint by moving the motion-profile
distance along the negative inward blade direction. Contact receives exactly
three `CARTESIAN_SEGMENT` moves. Engage and slice use absolute root-frame
targets. Outward retract signs a displacement equal to the profiled distance
along the negative inward blade direction, transformed into Basic-root axes.
Contact resolves that displacement from the fresh measured controlled-effector
position when retract is accepted. Therefore an unreachable slice endpoint
cannot turn extraction into a correction toward a stale planned endpoint.
Each segment follows a sequential-IK Cartesian approximation at Basic's
advertised control cadence and can be replaced immediately by the next signed
move. All use the same orientation and force-capacity vector and emit zero
rotational wrench.

If the profiled blade component is `L` kgf, the orthonormal blade, slicing, and
third-axis components are `L`, `0.5 L`, and `0.5 L` kgf, converted with
`1 kgf = 9.80665 N` and transformed to the Basic root frame. The Skill-owned
motion profile also supplies retract distance, engage delay, planned slice wait
rate, and retract delay. Provider results remain command dispositions and do
not assert cutting success.

The mounted-effector profile does not declare one universal blade direction.
Its namespaced Slicing extension contains numbered use profiles so the same
blade can retain multiple explicit effector-frame blade/slicing direction
pairs. Each use profile may also own hard joint locks. Agent tools send a null
selector unless the user explicitly requests a number; null resolves through
each profile store's live declared default. Profile number `#1` has no special
runtime privilege.

## 11. Carry-bound extension

`contact_work_plan.v2.schema.json` adds exactly one `carry` object to the v1
plan fields. `PREPARE` starts a not-yet-confirmed carry approach. `CONTINUE`
must match the currently confirmed carry ID and attachment revision; it may
replace the finite session while Contact retains the same Basic arm-group
lease and latest endpoint.

The first accepted Contact endpoint primes and activates Basic's arm-group
`POSITION_EFFORT_LIMITED` guard. That guard and the continuously streamed or
latched position/effort setpoint remain active across every subsequent Contact
move. Carry confirmation is a separate operation after the final signed
approach step is accepted and measured joint error/velocity satisfy the
settling gate; it verifies and retains the already-active guard rather than
introducing a new mode transition.
While confirmed, inactivity, authorization expiry, and endpoint replacement
gaps hold the last arm target rather than selecting gravity float. Explicit
relaxation clears the guard before group float and lease release. Contact does
not infer gripper contact, bind object metadata, or apply the temperature gate.
