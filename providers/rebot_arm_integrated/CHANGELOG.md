# Changelog

This file records release-level outcomes, not every development step.
Historical entries may use the names current at the time. Use the
[current terminology map](README.md#control-terminology), `manifest.json`, and
the live capability response for implementation work.

## Unreleased

## 0.9.1 - 2026-08-20

- Increase authorized-transit joint endpoint completion slack to `0.04 rad`
  for all six arm joints. This covers the owner-observed `0.03559 rad`
  alignment residual without weakening collision checks, IK acceptance, path
  authorization, velocity settling, or the no-progress watchdog.
- Migrate the unchanged prior arrival tolerance through managed policy
  revision 10, while preserving an explicitly customized tolerance.
- Accept the exact V3 canonical mounted-camera calibration policy emitted by
  Locate Arm Base anywhere the controller already accepts V2 canonical-camera
  identity semantics. Preserve V1 epoch binding and reject unknown versions.
- Relax the bounded shadow transit-planning time budget from 1.5 to 3.0
  seconds for long exact-pose moves. Collision validation, IK residual limits,
  and physical authorization gates remain unchanged.
- Accept the canonical `HAND_ANGULAR_4PI` scene layer and its 5 mm minimum
  sphere radius. The existing capsule/sphere collision evaluator consumes the
  projected hits without adding another collision primitive or planner.
- Calculate weighted Jacobian quality before the IK solver's already-satisfied
  early return. Replanning an orientation already within tolerance no longer
  reports a synthetic zero singular value and rejects the no-op alignment.
- Consume Basic's public mode-specific command limits instead of calibration
  and attended developer-test fields. The ordinary no-speed-request duration
  is now 1.5 seconds, and shorter requested durations continue to be stretched
  to a Basic-limit-feasible plan. Piecewise-linear authorized stages now use
  their actual linear velocity when computing that lower bound, allowing a
  too-fast request to execute close to the Basic cap instead of retaining an
  inapplicable 1.5x smoothstep margin.
- Replace the signed free-space executor's 10 Hz latched-waypoint handoffs with
  a controller-paced 50 Hz interpolation stream. Stage durations now honor the
  requested Cartesian speed, complete command ticks, and the selected Basic
  mode's advertised limits. The stream consumes Integrated's existing 50 Hz
  Basic feedback cache instead of serializing a second state HTTP request
  before every command; late command cycles still preserve samples and slow
  without burst catch-up. Final stability counts only new Basic observations.
- Make `IMPEDANCE` the explicit default signed-path backend and add a signed
  `POS_SPEED` option that emits 50 Hz Basic `POSITION_VELOCITY_LIMITED`
  position targets with backend-specific velocity-limit timing.
- Migrate dormant arm-contact and gripper POSITION_EFFORT_LIMITED command
  construction to Basic's N·m torque contract while retaining existing
  Integrated-internal ratio policy and telemetry.
- Record the operator-reported successful execution of several development
  6-DoF free-space motions with the `5 inch blade` assembly. The final
  checked-in profile uses 0.33 kg and COM `[-0.165, 0.0, -0.03]` m; this result
  accepts the patch behavior but does not promote the development collision
  envelope or full operating range to physical qualification.
- Remove the obsolete `/v1/preview` and `/v1/motion/plan` Agent-facing routes;
  autonomous free-space motion now has one signed path-plan/path-commit API.
- Remove the retired manual engagement, teleoperation, mutable runtime
  settings, gripper/contact, and Fabric Cartesian-target routes from the live
  catalog and HTTP surface. Public state now hides their dormant extraction
  internals, and the developer page is observation-only except for float and
  safe termination.
- Remove synthetic clearance-Z and lateral candidates. Until a general
  obstacle rerouter is implemented, planning follows the direct Cartesian path
  or its closest collision-free prefix.
- Consume the active assembly profile's mounted-effector collision spheres for
  path checking and expose their profile revisions in diagnostics.
- Make Agent free-space execution autonomous while preserving exact signed,
  one-time path authority; generic 6D motion now uses controller-owned
  `/path-plan` and `/path-commit` instead of legacy endpoint engagement.
- Ignore `PUSHABLE` under the current temporary policy, enforce 10 mm extra
  clearance for `KEEP_OUT`, and use zero extra margin for `WORK_OBJECT` while
  still prohibiting geometry intersection.
- Evaluate the direct Cartesian path and, when blocked, select its executable
  closest-safe collision-free prefix. General obstacle rerouting remains
  explicitly unimplemented so contact work cannot leak into this controller
  and the arm cannot invent a high-clearance detour.
- Make semantic-scene collision evidence opportunistic for free-space motion.
  Fresh scenes still reject colliding paths, while a missing or stale scene is
  bound and audited as `null` instead of blocking preview or commit.
- Recast Integrated as a free-space-only controller. Embedded contact and
  gripper paths remain compatibility code but are disabled and no longer
  advertised.
- Consume the Basic-resolved assembly profile, including controlled-frame
  geometry, collision radii, arm-group resource identity, and preview-bound
  assembly revisions.
- Consume mounted-effector collision spheres from the effector profile and
  keep the arm capsule profile independent of the selected gripper or tool.
- Lease only `robot_arm.primary/arm`, allowing a separate gripper owner to
  retain `robot_arm.primary/gripper` concurrently through Basic's disjoint
  group fencing.
- Expand the Agent adapter from single-axis/yaw inputs to one arbitrary 3D
  displacement vector plus controlled-frame-relative RPY or absolute
  arm-base RPY targets while preserving preview/commit authorization.
- Resolve advisory Manager authority from the active assembly's arm group so
  sibling gripper/contact groups are not unnecessarily claimed.
- Preserve exact local control-audit records while projecting oversized Fabric
  copies to bounded digest-bearing events, preventing one large plan result
  from blocking all later audit replay.

- Made `TRANSIT_SPEED` the ordinary-motion default and added
  `robot.motion.arm.integrated.pos_vel.one_shot`; retained the former
  `_limited` name as a deprecated alias.
- Replaced small Cartesian and aggregate-travel proxies with the 1.2 m request
  envelope plus actual IK, calibrated joint, singularity, semantic-scene, and
  motor/provider speed limits. Requests above the policy threshold require
  authentication or are rejected before execution.
- Added bounded relative controlled-frame deltas and support for
  `MOUNTED_CANONICAL_CAMERA_CALIBRATION_GATED_V2`, while retaining the
  identity/VIO-gated V1 compatibility policy.
- Revalidated every stored waypoint against the newest accepted semantic
  scene at commit instead of requiring one frozen scene revision.
- Added preview-bound `FLOAT`, `FIXED`, and bounded `WAIT_FOR_NEXT` terminal
  states for consecutive signed paths.
- Added a bounded higher-stiffness terminal-settling phase for one-shot
  impedance motion while retaining measured arrival and fallback checks.

## 0.8.3 - 2026-08-03

- Executed signed controller-owned transit waypoints through rate-limited
  impedance while Basic retained gravity-feed-forward authority.
- Allowed a transit preview to request 6-DoF IK without mutating the global
  operator mode.

## 0.8.2 - 2026-08-03

- Added leased gravity-float, compliant-hold, and position-lock idle profiles
  with measured endpoint capture and return to gravity float on release,
  expiry, failure, or motion supersession.

## 0.8.1 - 2026-07-31

- Corrected the configured joint-3 single-commit envelope needed for the
  reviewed upward safe-home test and exposed per-joint rejection diagnostics.

## 0.8.0 - 2026-07-29

- Added controller-owned nonphysical transit planning and exact, short-lived,
  one-time signed physical commit with commit-time identity, lease, inhibit,
  scene, collision, and measured-start revalidation.
- Added adaptive waypoint continuity, final endpoint hold/release, local exact
  control auditing, and asynchronous Fabric audit publication.
- Added observation-only Manager-authority versus Integrated-writer versus
  Basic-lease comparison with separate fencing namespaces.
- Completed one guarded OpenAI Agents SDK transit through the signed boundary;
  later authority loss correctly returned the arm to Basic gravity support.

## 0.7.0

- Established the current Basic/Integrated split, Provider-local environments,
  fenced Basic leasing, payload forwarding, measured/commanded telemetry, and
  authoritative safe termination.
- Added 3-DoF/6-DoF IK, controlled-frame offsets, Fabric target staging,
  capability readiness, nonphysical preview, and operator-gated MIT and
  latched endpoint profiles.
- Added attended gripper MIT/effort-limited tests and experimental
  CONTACT_WORK with a separate gravity-float baseline and explicit effort
  budgets. Experimental profiles remained outside Agent discovery.
- Added endpoint keepalive/mode-transition coordination with Basic, explicit
  completion-versus-arrival reporting, and return-to-float behavior.
