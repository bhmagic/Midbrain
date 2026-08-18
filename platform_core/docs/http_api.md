# Local HTTP API — Core 0.3.0

These are prototype local interfaces. They are intentionally simple and are not the final authenticated or version-negotiated transport.

## Resource Provider Manager — `http://127.0.0.1:7001`

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Systemic read-only mainframe for Manager, Fabric, Providers, Skills, and Agent UI links |
| GET | `/health` | Manager health and feature flags |
| GET | `/v1/ui/overview` | Aggregated Manager/Fabric health plus Provider and Skill liveness summaries |
| GET | `/v1/ui/robot-assembly/effectors` | Installed arm-compatible mounted-effector profiles, active identity, inertial tuning fields, and restart policy |
| POST | `/v1/ui/robot-assembly/effectors` | Confirm and persist one installed mounted-effector identity while the arm Provider and all transitive dependents are stopped |
| GET | `/v1/ui/providers/{id}` | Provider process, heartbeat, readiness, capabilities, streams, and latest observations |
| GET | `/v1/ui/skills/{id}` | Skill availability, lifecycle history, streams, and latest observations |
| GET | `/observe/provider/{id}` | Read-only Provider observation page |
| GET | `/observe/skill/{id}` | Read-only Skill observation page |
| GET | `/developer/provider/{id}` | Explicit confirmation boundary before resolving a Provider developer UI |
| GET | `/developer/skill/{id}` | Explicit confirmation boundary before resolving a Skill developer UI |
| POST | `/v1/ui/developer/{kind}/{id}/activate` | Confirmed Provider HOT/developer-UI activation or Skill developer-UI start |
| GET | `/shutdown` | Whole-workspace shutdown confirmation page |
| POST | `/v1/ui/shutdown` | Confirmed delegation to the dependency-aware workspace shutdown supervisor |
| GET | `/v1/providers` | Configured providers, process state, and latest report |
| GET | `/v1/agent-runtime-catalog` | Every configured Provider and advertised capability projected to regulated Agent lifecycle/readiness fields |
| GET | `/v1/providers/{id}/detail` | Complete current Manager `ProviderView` for one exact Provider ID; Agent hosts must sanitize diagnostic values before model exposure |
| GET | `/v1/capabilities` | Capability-specific availability derived from provider heartbeats |
| POST | `/v1/capability-bindings` | Create a non-enforcing deterministic capability-to-provider snapshot |
| GET | `/v1/capability-bindings/{id}` | Inspect one advisory binding by opaque binding ID |
| POST | `/v1/control-authority/leases` | Acquire an advisory resource-authority lease |
| POST | `/v1/control-authority/leases/{id}/renew` | Renew the current advisory lease |
| POST | `/v1/control-authority/leases/{id}/release` | Release and retain advisory lease history |
| GET | `/v1/control-authority/resources/{id}` | Inspect active authority and latest fencing generation |
| GET | `/v1/workcell-calibrations` | Inspect enforced workcell calibration activations and their state |
| POST | `/v1/workcell-calibrations/activate` | Activate one exact reviewed stationary-workcell candidate |
| POST | `/v1/workcell-calibrations/refine-translation` | Compare-and-swap one Skill-accepted XYZ-only update while preserving active rotation |
| POST | `/v1/workcell-calibrations/{id}/revoke` | Revoke one activation and publish the non-motion-usable edge |
| POST | `/v1/shutdown/plan` | Build and publish a Manager-owned shutdown dry run |
| GET | `/v1/shutdown` | Inspect the latest shutdown dry run |
| POST | `/v1/shutdown/{id}/execute` | Start the gated Manager-owned provider shutdown sequence |
| GET | `/v1/shutdown/executions/{id}` | Poll one accepted shutdown execution |
| POST | `/v1/providers/register` | Register one provider instance |
| POST | `/v1/providers/heartbeat` | Refresh lifecycle, health, readiness, and capabilities |
| POST | `/v1/providers/{id}/start` | Start the configured provider process |
| POST | `/v1/providers/{id}/hot` | Resolve declared dependencies in topological order, ensure each process is running, and request HOT residency |
| POST | `/v1/providers/{id}/warm` | Request WARM residency |
| POST | `/v1/providers/{id}/stop` | Request graceful stop; terminate on timeout only when that provider permits automatic force termination |
| POST | `/v1/providers/{id}/kill` | Force process-tree termination |

A provider heartbeat is expired after its configured `heartbeat_timeout_ms`. Expiry forces `ready=false`, marks the report `UNHEALTHY`, removes capability availability, and publishes an unavailable status observation to the Fabric.

Provider configuration may declare `dependencies` as provider IDs. A HOT
transition recursively starts and transitions those dependencies before the
requested provider, deduplicates shared dependencies, and rejects unknown IDs
or dependency cycles. The response retains the requested provider's result and
adds `manager_hot_dependencies` for audit. This lifecycle dependency mechanism
does not bypass capability binding, readiness, or request-specific data checks.

The aggregation endpoints and `/observe/*` pages are read-only. Their
developer links do not grant authority: they present an explicit overstepping
confirmation. The distinct confirmed activation endpoint may request Provider
`HOT` and start an advertised development process. For a Skill it starts only
the developer UI, not the finite Skill operation. Authentication and
authorization remain the responsibility of the developer surface and its
underlying control APIs.

The Agent runtime catalog is also read-only. It preserves complete Provider and
capability coverage without copying arbitrary heartbeat diagnostics, launch
commands, or environment values into routine model context. The per-Provider
detail endpoint returns Manager's existing complete record and therefore does
not create another state owner. It does not perform model-facing sanitization;
the Agent host applies credential-like redaction and may select one JSON pointer
before exposing the record. Neither endpoint changes lifecycle state or grants
request authority.

The shutdown UI starts `stop_workspace.ps1` itself with a 750 ms response delay;
it does not duplicate the Provider shutdown algorithm. Its request uses the
exact `SHUT_DOWN_MIDBRAIN` confirmation phrase and returns before that script
stops Manager.

The mounted-effector selector mutates only the machine-local central assembly
selection. It discovers profiles under the selected arm Provider, filters by
the selected arm model identity/revision, and accepts an exact profile ID and
revision rather than a client-supplied filesystem path. The request requires
`physical_effector_confirmed=true`. It returns `409 Conflict` with
`blocking_providers` while Basic or any transitive Provider dependent is still
loaded. A successful change keeps a recoverable `.previous` selection and
requires the affected Providers to restart before the profile becomes runtime
state. Profile mass or COM edits have the same restart requirement.

Provider configuration accepts `graceful_stop_timeout_ms`,
`force_kill_on_stop_timeout`, `safe_state_request_path`, and
`safe_state_timeout_ms`. `force_kill_on_stop_timeout` defaults to `true`. When
it is `false`, a graceful-stop timeout returns an error and leaves the process
running; an explicit `/kill` request is still authoritative. A safety-support
provider participating in Manager shutdown must have a safe-state route.

### Advisory capability binding

`POST /v1/capability-bindings` accepts semantic capabilities rather than
provider brands:

```json
{
  "required_capabilities": [
    "camera.rgb",
    "camera.depth_aligned_to_rgb"
  ],
  "required_resources_by_capability": {},
  "fallback_provider_ids": {
    "camera.rgb": "camera.femto_bolt",
    "camera.depth_aligned_to_rgb": "camera.femto_bolt"
  },
  "allowed_provider_ids": [],
  "excluded_provider_ids": [],
  "request_id": "request-123",
  "related_skill_id": "skill-123"
}
```

The Manager selects currently advertised, ready, healthy, HOT providers
deterministically. A request may bind a capability to a canonical resource
group, such as `robot_arm.primary/arm`; in that case the selected Provider
heartbeat must advertise that exact resource group. An explicit provider is considered only when no available
candidate exists for that capability. A fallback may be returned with
`requires_activation=true` and `compatibility_verified=false`, preserving the
existing hard-coded route during migration.

An enforcing consumer must reject that cold fallback state. Activation is a
separate provider lifecycle action, not an implicit effect of binding. After
the same explicit provider is HOT, ready, healthy, and advertising the required
capability, a new binding resolves it as `CURRENT` with
`selection_reason=AVAILABLE_CAPABILITY`,
`requires_activation=false`, and `compatibility_verified=true`. The consumer
must retain the configured fallback provider ID in its provenance and
revalidate the returned provider instance and boot before consequential use.

Bindings are `ADVISORY` and identify a provider instance and boot snapshot.
They do not start a provider, grant control authority, or change the existing
`/v1/providers/{id}/request` route. The Manager stores the binding in memory and
best-effort publishes the same record to `manager.capability_binding` on the
Fabric.

Creation validates the snapshot immediately. Every
`GET /v1/capability-bindings/{id}` revalidates it against the current provider
instance, boot, readiness, health, and residency. The `validity` field is one
of `CURRENT`, `STALE_PROVIDER_RESTARTED`, `STALE_PROVIDER_UNAVAILABLE`,
`UNRESOLVED`, or `FALLBACK_REQUIRES_ACTIVATION`; `validation_issues` gives
machine-readable reasons. Revalidation is visible but remains non-enforcing.

### Advisory control authority

`POST /v1/control-authority/leases` accepts a resource, owner, permissions,
duration, renewal interval, optional related Skill, and explicit preemption:

```json
{
  "resource_id": "robot_arm.primary/arm",
  "owner_id": "skill-execution-123",
  "permissions": ["plan", "execute"],
  "duration_ms": 6000,
  "renewal_interval_ms": 1000,
  "preempt": false,
  "related_skill_id": "skill-123"
}
```

Only one active advisory lease is retained for an overlapping resource scope
unless explicit preemption is requested. A parent scope conflicts with every
descendant, while siblings such as `robot_arm.primary/arm` and
`robot_arm.primary/gripper` may be leased concurrently. Each new acquisition increments its fencing
generation. Expired, released, and preempted records remain inspectable.
Resource views are best-effort published under
`manager.control_authority.<resource>`.

This interface is observational in Phase 1. Providers must not treat it as
physical authorization until the enforcement phase is explicitly enabled and
conformance-tested.

### Enforced stationary-workcell calibration

`POST /v1/workcell-calibrations/activate` accepts the immutable version-3
stationary-alignment candidate, its exact append-only approval record, a
signed reviewer-identity assertion, `request_id`, `activated_by`, and a
mounted-rig validity policy. It does not accept a wall-clock activation
lifetime. Manager requires
`MIDBRAIN_REVIEW_AUTH_SECRET` and verifies:

- candidate and approval schema/state plus exact canonical candidate SHA-256;
- signed reviewer identity, nonce, issue/expiry, and candidate binding;
- current mounted camera canonical-device/calibration identity and health;
- historical VIO provenance used to establish the gravity-aligned stationary
  world frame, without making a later VIO process restart a freshness gate;
- parent-from-child world/VIO/arm-base frame semantics;
- the exact single centered-mesh base-orientation proof: current world up,
  matching identity/X-180/Y-180/Z-180 choice, correction count zero or one,
  zero mesh-correction translation, and preserved CAD mesh center;
- a serialized `world_from_base` quaternion whose +Z is upward and matches the
  reviewed corrected up dot product; and
- proof that the immutable approval decision was recorded before the
  candidate's review deadline. A reviewed mounted calibration may be activated
  after that deadline if its stable camera identity and calibration still match.

Projected-size, confidence, bounded-error, support-plane, and residual-tilt
values remain review evidence but are not independent Manager geometry gates.

Only one activation remains `ACTIVE`. Its `expires_at_us` is null and its
`validity_policy` is `MOUNTED_CANONICAL_CAMERA_CALIBRATION_GATED_V2`. Temporary
camera unavailability suspends `motion_usable` without erasing the calibration;
recovery of the same canonical camera and calibration restores it. Camera or
VIO process instance/boot changes and a new VIO tracking epoch do not invalidate
the mounted transform. A canonical camera-device or camera-calibration revision
change does invalidate it. A successful newer activation publishes
its motion-usable static transforms and then marks the prior active record
`SUPERSEDED` and non-motion-usable without publishing a transient empty
calibration. Reusing a `request_id` is idempotent only for byte-equivalent
canonical content. `GET /v1/workcell-calibrations` re-evaluates current
stable camera identity, health, and calibration evidence before returning
records.

`POST /v1/workcell-calibrations/{activation_id}/revoke` requires
`request_id`, `revoked_by`, and `reason`. It publishes a newer authoritative
non-motion-usable observation before storing the `REVOKED` state. Fabric
static-transform selection suppresses older active edges after this
revocation, even if the older transform observation remains in history.

### Compact XYZ-only workcell refinement

`POST /v1/workcell-calibrations/refine-translation` is the state-commit route
for a finite Skill that has already decided to accept an XYZ-only correction.
The request identifies the active calibration, expected flat refinement
revision, exact source and proposed `world_from_base` transforms, caller, and
the structured refinement artifact. It is not a general calibration solver or
a perception-quality endpoint.

Manager rejects a request unless the activation is enforced and motion-usable,
the expected revision and source transform are current, source and proposed
rotations are byte-identical, camera/VIO/world identities still match, the arm
Provider instance and boot remain healthy, and proposed translation equals
source translation plus the declared adopted delta. The finite Skill retains
ownership of capture-motion limits, VLM/depth quality, conditional review,
profile bounds, sample aggregation, and adoption policy.

Manager publishes the updated motion-usable transform to Fabric before
committing the in-memory revision. A Fabric publication failure leaves the
active record unchanged. Successful updates increment
`translation_refinement_revision`, derive a new calibration revision, and
append one compact journal entry. The journal is bounded to 32 entries and has
no recursive parent-state chain. Reusing `request_id` is idempotent only for
byte-equivalent canonical request content.

### Manager shutdown plan and gated execution

`POST /v1/shutdown/plan` accepts `owner_id` and `reason`. It returns an ordered
plan that fences new authority, requests motion-provider relinquishment,
confirms Basic safe state, stops ordinary providers, preserves safety-critical
support until confirmed safe, flushes audit copies, stops Fabric, and leaves
Manager termination to the workspace supervisor last.

The endpoint has `SHADOW_DRY_RUN` enforcement. It records and publishes the
plan but performs no provider or process stop.

Execution is separately gated by
`MANAGER_SHUTDOWN_EXECUTION_ENABLED=false`, which is the default. When
explicitly enabled, the caller submits:

```json
{
  "request_id": "shutdown-execution-123",
  "confirmation": "EXECUTE_MANAGER_PROVIDER_SHUTDOWN"
}
```

to `POST /v1/shutdown/{shutdown_id}/execute`. The Manager returns HTTP 202 and
an `execution_id`, fences new provider starts, HOT transitions, and authority
acquisitions, and runs the provider sequence asynchronously. Poll
`GET /v1/shutdown/executions/{execution_id}` with a caller-owned deadline.

The Manager first relinquishes motion providers, then calls the configured
Basic safe-state route, and only after an explicit termination acknowledgement
stops ordinary providers and Basic. A Provider response may distinguish
`safe_state_confirmed` from `termination_allowed`. The reference Basic Provider
uses safe-home on the first attempt. A later shutdown execution may permit
termination after a fresh movement-based stationary observation, regardless of
absolute joint position, or after Basic reports that control is already
unavailable and retaining its process cannot provide support. The latter keeps
`safe_state_confirmed=false` and reports the physical outcome as unknown.
Fabric and Manager remain supervisor-owned.
`AWAITING_SUPERVISOR` means the provider sequence completed;
`PARTIAL_FAILURE_AWAITING_SUPERVISOR` requires inspection before core stop;
`BLOCKED_SAFETY_SUPPORT_RETAINED` means Basic, Fabric, and Manager must remain
running for that execution. The state is terminal for that execution, but a
new explicit shutdown plan and execution may retry the Provider-owned
termination check while the authority fence remains in effect.

The supplied template remains disabled for new installations. This validated
development workspace enables it locally and makes Manager execution the
default `stop_workspace.ps1` path. The explicit local fallback remains
available and is required when Manager cannot prove the provider sequence.

## World State Fabric — `http://127.0.0.1:7002`

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Fabric health, stream count, and feature flags |
| POST | `/v1/observations` | Publish one observation |
| POST | `/v1/observations/batch` | Publish multiple observations |
| GET | `/v1/latest/{stream}` | Latest observation for a stream |
| GET | `/v1/recent/{stream}?limit=32` | Bounded recent history |
| GET | `/v1/snapshot` | Latest observation for every stream |
| GET | `/v1/streams` | Stream catalog with freshness and stale status |
| GET | `/v1/sync` | Timestamp-nearest multi-stream observation bundle |
| GET | `/v1/transforms` | Current graph-eligible transform-edge catalog |
| GET | `/v1/transform` | Composed transform at a requested timestamp |

Example synchronized query:

```text
/v1/sync?streams=camera.rgb.frame_ref,camera.depth.frame_ref,camera.imu.accel&anchor_stream=camera.rgb.frame_ref&max_delta_us=50000&require_all=false
```

The response includes the anchor timestamp, matched observations, per-stream deltas, missing streams, stale streams, and `complete`. Stale observations are excluded. When `require_all=true`, an incomplete bundle is returned with HTTP 409 so callers cannot silently treat partial state as complete.

Transform observations remain available through the ordinary stream APIs even
when they are review candidates. The transform graph and
`GET /v1/transforms` exclude observations that are invalid, expired,
explicitly `motion_usable=false`, or
`review_state=CANDIDATE_REVIEW_REQUIRED`. A review-required transform must
explicitly declare `motion_usable=false`. The graph therefore cannot be
bypassed by publishing the same parent/child edge on a `.candidate` stream.

`GET /v1/transform` accepts `from_frame`, `to_frame`, optional `at_us`,
optional `session_epoch`, optional `max_extrapolation_us`, and optional
`wait_for_bracket_ms`. Without `wait_for_bracket_ms`, the route retains its
immediate v0.3 behavior. With a positive value, Fabric waits on transform
publication events until the requested path is exact or interpolated with
zero extrapolation, an authority conflict is known, or the deadline expires.
The wait is capped at 30 seconds. On deadline, Fabric returns the same current
200 extrapolated result or 404 result that an immediate query would return;
callers therefore retain the existing status and response contract.

Fabric retains ordinary per-stream observation history independently from its
transform-query index. The defaults remain 256 observations per stream and
4096 transform observations per directed edge. Transform indexing changes
query cost but does not replace the raw observation APIs, discard intermediate
samples, lower publisher cadence, or alter insertion-order eviction.

## v0.3 additions

- `GET /v1/schemas`
- `GET /v1/transforms`
- `GET /v1/transform?from_frame=...&to_frame=...&at_us=...&session_epoch=...&wait_for_bracket_ms=...`
- `POST /v1/providers/:id/request`
- `GET /v1/motion/inhibit`
- `POST /v1/motion/inhibit/acquire`
- `POST /v1/motion/inhibit/release`
