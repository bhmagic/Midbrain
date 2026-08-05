# Controller-Owned Transit Planning and Authorized Execution

Status: Phase 5 enforced commit; preview remains nonphysical

## Purpose

`POST /v1/motion/path-plan` moves general transit decisions out of individual
Skills and into the Integrated controller. It evaluates:

- a direct Cartesian path;
- a clearance-Z path;
- positive- and negative-Y lateral alternatives intended to escape poor wrist
  singularity regions;
- sequentially seeded Cartesian IK continuity;
- minimum Jacobian singular value and joint-jump limits;
- semantic-scene collision clearance when a scene is present;
- provider joint-rate caps and a controller maximum Cartesian speed.

The requested Skill speed is an input, not an authority. The controller clamps
it to its configured range and lengthens the duration when Basic provider rate
caps require a slower path.

The target may contain either an absolute arm-base `position_m` or a bounded
`position_delta_m`, never both. Relative deltas are resolved from Integrated's
fresh measured controlled frame inside the preview. This lets a visual Skill
request a capped correction when an upstream FK transform is temporarily
missing without guessing the controller tool's absolute origin.

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

Callers that intend to create an operator decision must include
`request_context` with:

- `binding_id`;
- `camera_provider_id`, `camera_provider_instance_id`, and `camera_boot_id`;
- `workcell_transform_id`, `workcell_transform_revision`, and
  `workcell_transform_validity_policy` (currently
  `MOUNTED_IDENTITY_TRACKING_GATED_V1`);
- `vio_session_epoch`;
- `observation_timestamp_us` and `observation_expires_at_us`; and
- `scene_revision`.

Legacy nonphysical diagnostic calls may omit this object, but the returned
contract then reports `request_context_complete: false` and must not be used to
create authorization.

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
`X-Midbrain-Authorization` HMAC assertion issued for one approved UI decision.
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
  10 rad/s on any joint, rejects at or above 20 rad/s, and executes at the
  smaller of controller, Basic, and motor POS_SPEED caps; and
- advances only after stable measured arrival at the prior exact waypoint.

The controller sends one latched POS_VEL endpoint per stage. A healthy
completion follows the preview-bound `final_state`: `FLOAT` returns directly
to verified gravity float, `FIXED` holds until explicit release, and
`WAIT_FOR_NEXT` maintains the endpoint for a bounded chaining interval. A
compatible next signed commit reuses the fenced lease and impedance/gravity
support, but still begins from fresh measured joints and revalidates the whole
new path against the newest scene. Chain-wait expiry requests verified float.
Lease loss,
inhibit, platform failure, mode/feedback error, or stage timeout requests
gravity-float. The local append-only audit synchronously records the exact
request and authorization-token SHA-256; the raw token is never persisted and
Fabric receives only an asynchronous audit copy.

TODO: add an in-flight successor queue distinct from `WAIT_FOR_NEXT`. It must
allow path B to be previewed and authorized while path A is still executing,
bind B's start to A's reviewed terminal joint state and plan identity, then
recheck measured transition drift and the newest semantic scene at the A-to-B
boundary. The first implementation should be a bounded one-successor queue;
later multistep routing may generalize it without weakening per-path authority.
This queue is the controller-side latency buffer for agentic workflows: Fabric
observations, perception, and planning may complete at different times, while
the servo loop must never depend on synchronous Agent dialogue. Queue admission
uses the predicted terminal state; activation remains fenced by the measured
terminal state and newest Fabric scene.

The future slicing Skill should submit task geometry and motion intent, such as
the desired cut line and speed envelope. The Integrated controller should own
the resulting collision-aware, singularity-aware, rate-capped physical path.

## Multistep environment routing TODO

The current candidate set contains a few fixed direct, clearance-Z, and lateral
alternatives. It is not yet a general route search. Add a controller-owned
multistep planner that:

- accepts one high-level controlled-frame goal and task/contact policy;
- searches the current canonical semantic scene for a complete collision-free
  route rather than asking the Agent to choose each intermediate displacement;
- time-parameterizes the resulting joint path against the active qualified
  motion profile;
- previews and authorizes the complete route as one immutable plan;
- advances through exact measured-arrival stages while monitoring newer scene
  revisions;
- stops or chooses another already authorized branch when clearance becomes
  unsafe; and
- returns structured per-leg and replan evidence.

Local replanning must stay inside the authorized goal, contact policy,
workspace, speed/effort envelope, and expiry. It must not turn a no-contact
approach into contact, change the selected workpiece, or expand task authority.
This functionality reduces Agent commands and dialogue without moving planning
or physical authority into the Agent.
