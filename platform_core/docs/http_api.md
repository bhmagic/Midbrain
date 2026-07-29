# Local HTTP API — Core 0.3.0

These are prototype local interfaces. They are intentionally simple and are not the final authenticated or version-negotiated transport.

## Resource Provider Manager — `http://127.0.0.1:7001`

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Manager health and feature flags |
| GET | `/v1/providers` | Configured providers, process state, and latest report |
| GET | `/v1/capabilities` | Capability-specific availability derived from provider heartbeats |
| POST | `/v1/capability-bindings` | Create a non-enforcing deterministic capability-to-provider snapshot |
| GET | `/v1/capability-bindings/{id}` | Inspect one advisory binding by opaque binding ID |
| POST | `/v1/control-authority/leases` | Acquire an advisory resource-authority lease |
| POST | `/v1/control-authority/leases/{id}/renew` | Renew the current advisory lease |
| POST | `/v1/control-authority/leases/{id}/release` | Release and retain advisory lease history |
| GET | `/v1/control-authority/resources/{id}` | Inspect active authority and latest fencing generation |
| GET | `/v1/workcell-calibrations` | Inspect enforced workcell calibration activations and their state |
| POST | `/v1/workcell-calibrations/activate` | Activate one exact reviewed stationary-workcell candidate |
| POST | `/v1/workcell-calibrations/{id}/revoke` | Revoke one activation and publish the non-motion-usable edge |
| POST | `/v1/shutdown/plan` | Build and publish a Manager-owned shutdown dry run |
| GET | `/v1/shutdown` | Inspect the latest shutdown dry run |
| POST | `/v1/shutdown/{id}/execute` | Start the gated Manager-owned provider shutdown sequence |
| GET | `/v1/shutdown/executions/{id}` | Poll one accepted shutdown execution |
| POST | `/v1/providers/register` | Register one provider instance |
| POST | `/v1/providers/heartbeat` | Refresh lifecycle, health, readiness, and capabilities |
| POST | `/v1/providers/{id}/start` | Start the configured provider process |
| POST | `/v1/providers/{id}/hot` | Ensure process is running and request HOT residency |
| POST | `/v1/providers/{id}/warm` | Request WARM residency |
| POST | `/v1/providers/{id}/stop` | Request graceful stop; terminate on timeout only when that provider permits automatic force termination |
| POST | `/v1/providers/{id}/kill` | Force process-tree termination |

A provider heartbeat is expired after its configured `heartbeat_timeout_ms`. Expiry forces `ready=false`, marks the report `UNHEALTHY`, removes capability availability, and publishes an unavailable status observation to the Fabric.

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
deterministically. An explicit provider is considered only when no available
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
  "resource_id": "robot_arm.primary",
  "owner_id": "skill-execution-123",
  "permissions": ["plan", "execute"],
  "duration_ms": 6000,
  "renewal_interval_ms": 1000,
  "preempt": false,
  "related_skill_id": "skill-123"
}
```

Only one active advisory lease is retained for a resource unless explicit
preemption is requested. Each new acquisition increments its fencing
generation. Expired, released, and preempted records remain inspectable.
Resource views are best-effort published under
`manager.control_authority.<resource>`.

This interface is observational in Phase 1. Providers must not treat it as
physical authorization until the enforcement phase is explicitly enabled and
conformance-tested.

### Enforced stationary-workcell calibration

`POST /v1/workcell-calibrations/activate` accepts the immutable version-2
stationary-alignment candidate, its exact append-only approval record, a
signed reviewer-identity assertion, `request_id`, `activated_by`, and a
bounded `duration_ms` from 1000 through 300000. Manager requires
`MIDBRAIN_REVIEW_AUTH_SECRET` and verifies:

- candidate and approval schema/state plus exact canonical candidate SHA-256;
- signed reviewer identity, nonce, issue/expiry, and candidate binding;
- current camera provider/instance/boot/calibration identity and health;
- current tracking VIO epoch/frame and observation freshness;
- parent-from-child world/VIO/arm-base frame semantics;
- confidence of at least 0.70, translation error no greater than 0.01 m, and
  rotation error no greater than 0.05 rad; and
- candidate and requested activation lifetimes.

Only one unexpired activation may be `ACTIVE`. Reusing a `request_id` is
idempotent only for byte-equivalent canonical content. A successful activation
publishes the motion-usable static transforms and an activation record to
Fabric. `GET /v1/workcell-calibrations` marks elapsed records expired before
returning them.

`POST /v1/workcell-calibrations/{activation_id}/revoke` requires
`request_id`, `revoked_by`, and `reason`. It publishes a newer authoritative
non-motion-usable observation before storing the `REVOKED` state. Fabric
static-transform selection suppresses older active edges after this
revocation, even if the older transform observation remains in history.

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
Basic safe-state route, and only after an explicit acknowledgement stops
ordinary providers and Basic. Fabric and Manager remain supervisor-owned.
`AWAITING_SUPERVISOR` means the provider sequence completed;
`PARTIAL_FAILURE_AWAITING_SUPERVISOR` requires inspection before core stop;
`BLOCKED_SAFETY_SUPPORT_RETAINED` means Basic, Fabric, and Manager must remain
running and the local authoritative safety fallback must be used.

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

## v0.3 additions

- `GET /v1/schemas`
- `GET /v1/transforms`
- `GET /v1/transform?from_frame=...&to_frame=...&at_us=...&session_epoch=...`
- `POST /v1/providers/:id/request`
- `GET /v1/motion/inhibit`
- `POST /v1/motion/inhibit/acquire`
- `POST /v1/motion/inhibit/release`
