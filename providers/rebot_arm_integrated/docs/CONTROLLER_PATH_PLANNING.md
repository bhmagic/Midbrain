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
  `workcell_transform_expires_at_us`;
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
  motion-usable, unexpired, and unchanged in camera/VIO identity;
- rejects a preview whose lifetime outlasted its workcell transform or source
  observation;
- reacquires or validates its fenced Basic lease and verifies gravity-float
  before the first endpoint;
- requires healthy Manager and Fabric status with no global motion inhibit;
- rejects controller identity/configuration changes and excessive measured
  start-joint drift;
- rechecks every exact joint waypoint, whole-path endpoint delta and aggregate
  joint travel;
- recomputes collision clearance against the current exact scene revision;
- limits every joint to the smaller of 0.25 rad/s and Basic's reported cap,
  while independently clamping Cartesian speed; and
- advances only after stable measured arrival at the prior exact waypoint.

The controller sends one latched POS_VEL endpoint per stage. A healthy
completion deliberately holds the final endpoint and control mode until
`POST /v1/motion/path-release` requests verified gravity-float. Lease loss,
inhibit, platform failure, mode/feedback error, or stage timeout requests
gravity-float. The local append-only audit synchronously records the exact
request and authorization-token SHA-256; the raw token is never persisted and
Fabric receives only an asynchronous audit copy.

The future slicing Skill should submit task geometry and motion intent, such as
the desired cut line and speed envelope. The Integrated controller should own
the resulting collision-aware, singularity-aware, rate-capped physical path.
