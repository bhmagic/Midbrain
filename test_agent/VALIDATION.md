# Validation

## v0.3.0 Phase 5 checkpoint

- Ninety-three Test Agent regressions pass from the current workspace source
  root.
- The complete stopped local software matrix count is recorded in
  `project_docs/PHASE5_PROGRESS.md`.
- Enforced spatial registration consumes a hardware-incapable replay mapping
  through the same shared-memory generation checks used by the live camera.
- Camera binding is revalidated after the synchronized payload copy; route,
  bundle, and calibration provider/instance/boot identities must agree.
- Each adapter applies its own source-time and association policy to passive
  Fabric observations. Recycled BufferRefs, provider restarts, mixed camera
  identities, non-tracking VIO state, VIO epoch crossings, and disallowed
  direct-route use are rejected.
- Enforced binding rejects a cold explicit provider fallback. After that same
  provider is independently active and current, the selected instance/boot and
  configured fallback ID remain visible in provenance.
- All four finite Agent SDK adapters are registered behind manifest
  allowlisting. Calibration remains approval-required; spatial and tool
  registration are read-only; none adds physical execution.
- All sixteen deterministic replay scenarios evaluate injected evidence and
  assert that hardware, physical leases, controller calls, and Agent execution
  remain unavailable.
- Capture validation enforces payload hashes and size limits, records a manual
  retention-review policy, and exposes a read-only browser provenance summary.
- Observation authorization requires a collision-free, current, digest-bound
  Integrated preview tied to the exact request, camera/workcell/VIO/scene
  context, controller instance/boot/configuration, and lease snapshot.
- Expired, incomplete, scene-mismatched, request-tampered, or
  controller-identity-tampered previews fail closed.
- An approved decision can mint one short-lived execution assertion for the
  exact controller preview. Approval and execution remain separate calls,
  assertion replay is rejected, and only the token hash is retained.
- A live nonphysical attempt on 2026-07-27 was blocked before frame capture by
  Windows Media Foundation error `0x80070005` while opening the Orbbec RGB
  device. Both arm providers remained stopped and no VLM or physical call was
  made.
- The 2026-07-29 publication run passed all 93 Test Agent tests from the clean
  staging tree using the Test Agent's declared monorepo source roots.
- The protected repository workflow retains its smaller legacy dependency and
  source-root set. Its exact Python command passes 111 tests and skips three
  Agents-SDK-only modules when the optional SDK is absent. The complete
  93-test Test Agent suite is part of the independently run publication
  matrix.
- The Test Agent test package also declares its own monorepo source roots and
  skips only Agents-SDK construction cases when that optional SDK is absent.
  Generic VLM routing remains importable without Google/OpenAI clients; each
  concrete hosted backend fails clearly only when selected without its SDK.

## v0.2.9

- JavaScript syntax validation covers initialization window count/rate diagnostics.


Validated in the delivery environment:

- Thirty Test Agent regressions.
- Manifest-only Skill discovery and explicit adapter allowlisting.
- Generic RGB-D route preference and direct provider fallback.
- Binding fallback and revalidation client contract.
- Decision-specific authorization that cannot execute an action.
- A real Integrated `SHADOW_NONPHYSICAL` transit preview is required before an
  observation-motion authorization can be created.
- Rejected or unavailable controller previews do not create authorization.
- Five initialization and point-cloud lifecycle regression tests.
- Recovery when reset changed epoch despite a transient control HTTP error.
- Waiting for VIO motion-inhibit acknowledgement before reset.
- Forced-reinitialization map lifecycle and pause during degraded VIO.
- Transient recycled BufferRef classification.
- Browser JavaScript syntax with Node.
- Orthographic isometric projection, world-down arrow, camera frustum, inertial propagation, visual correction, and gravity diagnostics.
- Python source compilation and wheel construction.

Still required on the target workspace:

- Automatic startup initialization reaches `SUCCEEDED` with Local VIO v0.2.2.
- High-rate inertial pose updates remain smooth between camera frames.
- Visual correction source and staleness match physical camera conditions.
- IR/depth fallback appears only when synchronized and materially stronger than RGB-D.
- Map remains spatially consistent during moderate and fast camera motion.


## v0.2.8

- Initialization display surfaces IMU history counts, timestamp skew, and the exact backend blocker.
- An earlier failed startup attempt is shown as superseded while a later initialization is active or successful.
