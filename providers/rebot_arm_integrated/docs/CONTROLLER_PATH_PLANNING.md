# Controller-Owned Transit Planning and Authorized Execution

Status: signed physical commit enforced; preview remains nonphysical

## Purpose

`POST /v1/motion/path-plan` moves free-space transit decisions out of
individual Skills and into the Integrated controller. It evaluates:

- a direct Cartesian path;
- sequentially seeded Cartesian IK continuity;
- minimum Jacobian singular value and joint-jump limits;
- semantic-scene collision clearance when a scene is present;
- provider joint-rate caps and a controller maximum Cartesian speed.

The requested Skill speed is an input, not an authority. The controller clamps
it to its configured range and lengthens the duration when Basic provider rate
caps require a slower path. Authorized path legs use piecewise-linear joint
interpolation, so the lower-bound calculation uses `abs(delta_q) / rate_cap`;
after 50 Hz tick quantization, an infeasibly short request therefore runs as
close to the applicable Basic limit as the complete-tick duration permits.

`execution_backend` is optional and defaults to `IMPEDANCE`. The only other
accepted value is `POS_SPEED`, which maps to Basic
`POSITION_VELOCITY_LIMITED`. The normalized value is included in the immutable
request and its digest. Preview, authorization, and commit therefore describe
one backend; commit cannot substitute the other backend.

The target may contain either an absolute arm-base `position_m` or a bounded
`position_delta_m`, never both. Relative deltas are resolved from Integrated's
fresh measured controlled frame inside the preview. This lets a visual Skill
request a capped correction when an upstream FK transform is temporarily
missing without guessing the controller tool's absolute origin.

The fresh-scene policy currently ignores `PUSHABLE`, applies 10 mm additional
clearance to `KEEP_OUT`, and applies zero additional clearance to
`WORK_OBJECT`. Zero additional margin is not contact permission: robot and
object geometry still may not intersect.

The collision consumer accepts canonical sphere layers named
`GRIPPER_0P5M`, `ARM_BASE_1P2M`, and `HAND_ANGULAR_4PI`. The last is a bounded
hand-centric level-of-detail producer profile, not a different collision
primitive: all hits arrive as ordinary base-frame spheres.

General obstacle rerouting is not implemented. The controller does not invent
a clearance-Z or lateral path because those templates can create surprising
motion unrelated to observed obstacle geometry. If the direct path cannot
reach the goal collision-free, the controller may return its collision-free
prefix ending as close as possible to the first semantic boundary. Such a plan
is executable but sets
`goal_reached: false`, `closest_safe: true`, and
`motion_outcome: CLOSEST_SAFE`, including the blocking object IDs/types and
remaining goal distance.

## Preview safety and latency boundary

This endpoint is direct HTTP and does not place Fabric in its synchronous path.
Its exact request and result are copied to the local control audit, with an
asynchronous Fabric outbox copy.

The planning endpoint:

- does not stage the physical target;
- does not engage the controller;
- does not acquire, replace, or release a lease;
- does not change a motor mode;
- does not submit a Basic motor command;
- always returns `physical_motion_authorized: false`;
- exposes `enforcement: SHADOW_NONPHYSICAL`.

## Freshness and identity contract

Observation-driven callers that intend to create a reviewed decision must include
`request_context` with:

- `binding_id`;
- `camera_provider_id`, `camera_provider_instance_id`, and `camera_boot_id`;
- `workcell_transform_id`, `workcell_transform_revision`, and
  `workcell_transform_validity_policy`;
- `observation_timestamp_us` and `observation_expires_at_us`; and
- `scene_revision`.

Two mounted-workcell policies are intentionally supported:

- `MOUNTED_IDENTITY_TRACKING_GATED_V1` additionally requires
  `vio_session_epoch` and remains bound to camera process/boot and VIO-session
  identity.
- `MOUNTED_CANONICAL_CAMERA_CALIBRATION_GATED_V2` requires the exact
  `camera_calibration_revision`. Its reviewed activation does not expire merely
  because a process or VIO epoch changes, but camera identity, calibration,
  activation state, or transform revision changes still invalidate use.

Callers must copy the policy and its required identity fields from the active
Manager workcell activation. They must not convert V1 evidence to V2, omit a
V1 epoch, or invent a calibration revision.

Diagnostic path-plan calls may omit this object, but the returned contract then
reports `request_context_complete: false` and cannot be committed.

Autonomous generic free-space motion instead uses
`context_kind=AUTONOMOUS_FREE_SPACE_KINEMATIC`. It binds the complete spatial
resolution and its canonical digest plus resolution time. This context is for
arm-base or already-resolved spatial goals; observation-driven item approaches
continue to use the stricter camera/workcell/scene context above.

`preview_contract` records a bounded issue/expiry interval, controller
provider/instance/boot/configuration identity, exact normalized request and
context digests, semantic-scene revision, current lease/fencing snapshot, and
Basic feedback identity. `preview_sha256` covers the complete planning result
and the contract before that digest is inserted. Consumers must independently
recompute all digests, reject missing or changed context, and require the
selected plan to be both planning-valid and collision-free.

The contract always records `physical_motion_authorized: false`,
`preview_grants_commit_authority: false`, and
`commit_endpoint_exposed: false`. The final field means this preview response
contains no commit capability or token; it does not claim that the provider has
no separately protected physical-control APIs.

## Separately authorized physical commit

`POST /v1/motion/path-commit` is not a mode on the planning request. It requires
the exact stored `plan_id`, `request_sha256`, and `preview_sha256`, plus a
`X-Midbrain-Authorization` HMAC assertion issued for one exact host policy
decision. Normal free-space Agent motion uses issuer
`physical-agent-autonomy` with resolver `autonomous-free-space-policy`; the
commit does not require a human approval dialog. UI-issued assertions remain
supported for attended workflows.
The assertion binds controller provider/instance/boot/configuration, plan,
request, preview, semantic scene, decision, resolver, and expiry. Both the
plan and assertion ID are consumed once.

Immediately before physical execution, Integrated:

- requires Manager to report the exact workcell calibration as active,
  motion-usable, and valid under its declared policy. The mounted canonical
  camera policy is calibration-gated and does not expire merely because a
  process or VIO epoch changes;
- rejects a preview whose lifetime outlasted its workcell transform or source
  observation;
- reacquires or validates its fenced Basic lease and verifies gravity-float
  before the first endpoint;
- requires healthy Manager and Fabric status with no global motion inhibit;
- rejects controller identity/configuration changes and excessive measured
  start-joint drift;
- rechecks every exact joint waypoint, whole-path endpoint delta and aggregate
  joint travel;
- recomputes collision clearance for every stored waypoint against the newest
  accepted exact scene. A safe revision advance is recorded, while a newly
  colliding path is rejected before the first endpoint;
- derives requested per-joint speed, requires explicit authentication above
  10 rad/s on any joint, rejects at or above 20 rad/s, and lengthens the
  timeline to remain within Basic's advertised limits for the signed backend;
  and
- quantizes every leg to complete 50 Hz command ticks before physical
  execution.

The controller streams a time-paced piecewise interpolation of the exact
authorized joint waypoints at 50 Hz. Intermediate waypoints are timing knots,
not measured-arrival gates, so the controller does not introduce a stop and a
10 Hz endpoint handoff at every knot. With the default `IMPEDANCE` backend,
each command includes the interpolated joint velocity target and a
Basic-bounded target-rate ceiling with tracking headroom. With `POS_SPEED`,
each command uses Basic `POSITION_VELOCITY_LIMITED` and includes the
interpolated position plus a Basic-bounded velocity limit; it does not carry an
impedance velocity target or gains. The command worker consumes the shared
50 Hz Basic feedback cache populated by Integrated's independent state poller;
it does not serialize another state HTTP request ahead of every command. If a
command cycle itself is late, the controller lengthens the physical timeline
instead of sending a burst or skipping a planned waypoint. Final-arrival
stability counts only distinct Basic observation timestamps.

A healthy completion still requires stable measured final position and
velocity and follows the preview-bound `final_state`: `FLOAT` returns directly
to verified gravity float, `FIXED` holds until explicit release, and
`WAIT_FOR_NEXT` maintains the endpoint for a bounded chaining interval. Final
holds use the configured 10 Hz keepalive because their target is stationary. A
compatible next signed commit reuses the fenced lease and Basic gravity
support, but still begins from fresh measured joints and revalidates the whole
new path against the newest scene. Chain-wait expiry requests verified float.
Lease loss,
inhibit, platform failure, mode/feedback error, or stage timeout requests
gravity-float. The local append-only audit synchronously records the exact
request and authorization-token SHA-256; the raw token is never persisted and
Fabric receives only an asynchronous audit copy.

General route search beyond the implemented direct/clearance candidates, an
in-flight successor queue, task-level slicing paths, and broader multistep
orchestration are not part of this implemented boundary.
Their design and promotion gates belong in the
[active roadmap](../../../docs/09_LIMITATIONS_AND_ROADMAP.md#controller-owned-multistep-routing),
not in this current-interface reference.
