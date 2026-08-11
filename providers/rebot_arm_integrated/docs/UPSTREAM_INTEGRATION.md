# Upstream integration

This is the current integration boundary for Skills, Agent hosts, and external
tools. Capability discovery does not grant physical authority.

## Discovery

Integrated publishes `details.capability_readiness` in its Manager heartbeat.
The canonical free-space capability is
`robot_arm.motion.free_space.preview_commit.v1`. The Provider-local
`GET /v1/capabilities` response maps that capability to the current HTTP
operations.

Callable operations are limited to:

- `GET /health`, `/v1/state`, `/v1/config`, `/v1/capabilities`, and
  `/v1/control-audit`;
- Manager lifecycle routes under `/v1/control/`;
- `POST /v1/motion/path-plan`, `/v1/motion/path-commit`, and
  `/v1/motion/path-release`;
- `POST /v1/float` and the leased idle-profile routes; and
- `POST /v1/safe-terminate`.

There is no upstream mutable Cartesian-target API. Runtime callers cannot
change the selected controlled frame, payload profile, execution backend,
contact policy, or gripper settings. Static identity and geometry come from
the Basic-published assembly state; controller timing and safety limits come
from configuration.

## Signed free-space transaction

1. Resolve Integrated for `robot_arm.motion.free_space.preview_commit.v1` and
   request HOT. Manager activates the declared Basic dependency transitively.
2. Submit one complete position or 6-DoF goal to `/v1/motion/path-plan`.
3. Verify the returned plan is nonphysical and retain its exact plan ID,
   request digest, preview digest, provider identity, configuration digest,
   assembly fingerprint, measured start, and expiry.
4. Authorize that exact path under the autonomous no-contact host policy.
5. Submit the assertion and exact digests to `/v1/motion/path-commit` before
   expiry.
6. Inspect the terminal outcome. Treat `CLOSEST_SAFE` as successful arrival at
   the reachable no-contact boundary, not destination arrival.
7. Release a retained final state explicitly when required.

The model-facing `perform_relative_effector_motion` tool performs these steps
inside one call-scoped host coordinator. The model never receives an
execution tool that accepts a plan ID. A new Agent turn clears any unconsumed
no-contact continuation, so an older turn cannot replay it.

## Scene and geometry inputs

The only Fabric motion-related input is the semantic scene stream
`robot_arm.primary.integrated.scene`. Integrated never treats a Fabric
observation as motion authority. A fresh scene is collision-checked at plan and
commit; a missing or stale scene is recorded as scene-blind operation under
the current policy.

The Basic assembly state provides:

- controlled-frame and mounted-effector transforms;
- effector mass, center of mass, and inertia;
- arm collision capsules and mounted-effector spheres;
- actuator group identity; and
- exact profile revisions and an assembly fingerprint.

Any change invalidates an outstanding plan. Unsupported effector primitive
shapes or frames fail closed.

## Autonomous lifecycle and authorization

Task-required start, HOT, and WARM transitions are host-authorized without a
human approval prompt. Stop remains governed by lifecycle safety because it
may remove powered gravity support.

Physical free-space execution still requires one signed, short-lived,
single-use assertion. “Autonomous” removes human interruption; it does not
remove exact path binding, lease fencing, motion-inhibit checks, measured-start
freshness, collision checking, or audit recording.

Every state-changing request is written to the Provider-local control audit
before controller submission. Fabric receives a later best-effort copy and is
not part of the synchronous motor-control path.
