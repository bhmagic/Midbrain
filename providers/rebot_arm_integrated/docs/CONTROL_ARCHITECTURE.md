# Integrated free-space control architecture

Integrated owns non-contact Cartesian arm movement. It converts an arbitrary
3D translation, orientation-only goal, or combined 6-DoF goal into one
controller-owned path. Intentional contact and gripper actuation are separate
controller domains.

## Separation of duty

| Component | Responsibility |
|---|---|
| Agent Skill | Resolve task intent and reference frames; never invent joint paths or replay a plan ID |
| Integrated Provider | IK, direct-path planning, collision checking, timing, signed commit, measured completion |
| Basic Provider | Hardware transport, group fencing, motor modes/limits, gravity support, assembly publication |
| Scene compiler | Produce canonical semantic collision geometry and filter current robot/tool geometry |
| Contact controller | Future force-guided cutting, pushing, pressing, and moment-to-moment visual steering |
| Grip controller | Future gripper actuation, grasp verification, and held-object attachment publication |

Integrated leases only the assembly-selected arm group. A grip controller may
eventually lease a disjoint gripper group concurrently. Any held object must be
published as versioned runtime attachment geometry before Integrated may move
the arm.

## Configuration and profiles

The central robot assembly selection references Provider-owned files for:

- arm model and calibration;
- mounted-effector transform, controlled frame, inertial properties, and
  collision primitives; and
- arm collision capsules and resource groups.

Basic resolves and validates those files, then publishes one assembly state
and fingerprint. Integrated does not load a second private arm model or permit
runtime replacement of the selected controlled-frame or payload profile.

Path rate, joint-speed ceilings, duration bounds, collision sampling, and idle
behavior are controller configuration. Hardware motor limits and gravity
support remain Basic/profile data. Agent prompts and Skills do not own either
class of limit.

## Motion transaction

1. `/v1/motion/path-plan` captures the current measured start, assembly,
   controller identity, configuration, goal, timing policy, and usable scene.
2. The planner interpolates Cartesian position and orientation, solves each IK
   waypoint from the preceding joint solution, and samples arm capsules plus
   mounted-effector spheres across joint segments.
3. The response contains immutable request and preview digests and a short
   expiry. Planning is nonphysical.
4. The host autonomously authorizes that exact no-contact path and issues a
   single-use signed assertion.
5. `/v1/motion/path-commit` revalidates identity, assembly, measured start,
   lease, inhibit, limits, path geometry, and the newest usable scene before
   motor submission.
6. Completion reports measured arrival, physical outcome, final state, and
   whether the requested destination or only a closest-safe boundary was
   reached.

The model-facing tool performs this transaction inside one call-scoped host
coordinator. It never exposes a separate execution tool with a selectable plan
ID. A new Agent turn discards any unconsumed no-contact continuation.

## IK and trajectory backends

Integrated supports position-only 3-DoF IK and complete pose 6-DoF IK. A
multi-axis translation is one vector goal; it is not decomposed into
single-axis moves. Controlled-frame RPY deltas compose onto the measured
controlled orientation; absolute RPY goals are expressed in `rebot_arm_base`.

Signed free-space paths default to Basic `IMPEDANCE`. A caller may instead
select `execution_backend: POS_SPEED`, which maps to Basic
`POSITION_VELOCITY_LIMITED`. The normalized backend is part of the immutable
request digest and cannot change between preview and commit. Integrated
converts requested Cartesian speed and planned Cartesian waypoint distances
into a duration for every joint-space leg, lengthens any leg that would exceed
the selected Basic mode's advertised joint limits, and quantizes those
durations to its configured 50 Hz command cadence. `IMPEDANCE` streams
interpolated position and velocity targets plus bounded target rates;
`POS_SPEED` streams interpolated position targets plus bounded velocity limits.
Requested Cartesian speed is a nominal average path speed, not a guarantee of
constant instantaneous Cartesian velocity.

Every planned IK waypoint remains on the streamed path. Integrated does not
wait for measured arrival at intermediate waypoints; it confirms stable
measured position and velocity only at the final endpoint. A late command
cycle slows the timeline rather than bursting commands to catch up. Basic
stages motor-mode changes, enforces its final rate ceilings, and preserves
gravity-supported control for other joints during transition.

## Semantic collision policy

The scene uses stable base-frame spheres and object IDs:

- `KEEP_OUT`: blocking, with 10 mm extra clearance;
- `WORK_OBJECT`: blocking at the geometric boundary, with 0 mm extra
  clearance; and
- `PUSHABLE`: ignored by the current temporary free-space policy.

`WORKPIECE` is normalized to the controller's `WORK_OBJECT` representation.
Classification never authorizes contact. Unclaimed depth remains non-blocking
`PUSHABLE` telemetry under the current explicit policy.

Canonical scenes may use `HAND_ANGULAR_4PI` in addition to the legacy
`GRIPPER_0P5M` and `ARM_BASE_1P2M` scopes. The angular scope is hand-centric at
scene-production time, but its resulting centers and radii are frozen in
`rebot_arm_base` for the immutable preview. Integrated performs the same
capsule/sphere and sphere/sphere clearance calculations for all three scopes.

The scene compiler uses the active assembly's arm capsules and effector spheres
for robot-self exclusion, preventing the selected tool from reappearing as a
work object. It publishes those exact sphere revisions with the scene so the
controller and main 3D viewer share one geometry source.

A fresh scene is checked at plan and commit. A missing or stale scene is
recorded as scene-blind operation under the current policy rather than causing
a semantic-only refusal. A newly detected collision still invalidates the
commit.

## Route selection

The current planner evaluates the direct Cartesian path only. It does not
generate clearance-Z, lateral, or other synthetic detours. If the direct path
is blocked after a nonempty safe prefix, Integrated may select that prefix and
report `CLOSEST_SAFE`. If the start is already unsafe or no executable prefix
exists, it rejects without motion.

General obstacle rerouting remains an explicit future module. It should be
added as a bounded planner behind the same immutable preview/commit contract,
not as Agent-generated waypoints.

## Final states and fallback

A signed plan selects `FLOAT`, `FIXED`, or bounded `WAIT_FOR_NEXT` as its final
state. Consecutive correction plans still require a fresh measured start and a
new signed assertion. An expired wait returns to gravity float.

Transport uncertainty, lost lease, stale feedback, inhibit, Basic fault,
changed assembly, collision, or execution failure blocks new commands and
requests float when authority remains valid. The Provider does not
automatically retry physical motion after a safety fallback.
