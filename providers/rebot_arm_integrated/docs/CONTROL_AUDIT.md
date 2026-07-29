# Provider-Local Control Audit

Status: `STRICT_LOCAL` pre-action enforcement enabled and guarded-physical
validated in the current workspace; installation template remains shadow

The Integrated controller records the exact canonical request submitted to
each state-changing control endpoint before executing that endpoint. Accepted
and rejected outcomes are recorded as separate lifecycle events. This gives
the development UI and later authorization UI an inspectable record without
putting the Fabric in the synchronous controller path.

Each event contains:

- an immutable audit event ID and provider-local sequence;
- provider instance and boot IDs;
- endpoint, command ID, plan ID, binding ID, authority ID, and related Skill ID
  when supplied;
- the canonical request and its SHA-256 digest;
- `SUBMITTED`, `ACCEPTED`, or `REJECTED` lifecycle;
- normalized result or error.

The default append-only file is
`runtime_logs/control_audit/events.jsonl` beneath the provider directory.
Pending events are copied asynchronously to
`robot_arm.integrated.control_audit`. A cursor allows unpublished events to be
replayed after restart. A Fabric outage does not block direct controller
submission.

Each endpoint response reports whether the `SUBMITTED` and `ACCEPTED` local
copies persisted and includes any post-action audit error. The default
`SHADOW_BEST_EFFORT` mode reports local write failures but does not reject
controller work.

`STRICT_LOCAL` with `strict_local_write=true` enforces only the pre-action
boundary: if the exact `SUBMITTED` record cannot be persisted, the controlled
operation is not called. Once the controlled target has returned, an
`ACCEPTED` write failure is observable but cannot retroactively change the
operation result to rejected. Likewise, failure to append `REJECTED` must not
mask the original controller error. Fabric publication remains asynchronous in
both modes.

The current workspace enabled strict mode after the best-effort history was
checked for Fabric delivery gaps, strict write-failure tests passed, and a
guarded `+0.03 m Z` physical round verified that Basic gravity support and
error-to-float behavior remained intact. The run ended with local and Fabric
sequence `261`, zero pending records, and no local or Fabric audit errors. The
installation template intentionally remains in shadow so strict local storage
is not imposed on unvalidated installations.

`POST /v1/motion/plan` is the initial direct planning boundary. It stages and
previews a target in one provider call, returns a plan ID and normalized target,
and cannot authorize physical motion. Collision preview remains diagnostic in
this phase.
