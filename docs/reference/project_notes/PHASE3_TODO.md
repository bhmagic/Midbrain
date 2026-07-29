# Phase 3 Systemic Interconnection and Progressive Enforcement

Date: 2026-07-27
Status: active; validated Manager shutdown execution is enabled in this
workspace, while other enforcement boundaries remain staged

## Goal

Convert the independently testable Phase 1 and Phase 2 shadow paths into the
normal interconnections, then enable one policy at a time. Compatibility
fallbacks remain available until the corresponding generic route has passed
both software and guarded physical validation.

## Gate 0: close Phase 2 physical findings

- [x] Reject preview IK position residuals above the configured tolerance.
- [x] Reject POSE_6DOF orientation residuals above the configured tolerance.
- [x] Recheck residuals on the fresh physical commit solve and on live replan.
- [x] Do not classify a zero-length plan as singular merely because sigma is
  unavailable or zero.
- [x] Serialize Integrated lease acquire, renew, deliberate WARM/COLD release,
  and lease-loss recovery.
- [x] Require TRANSIT_SPEED joint arrival, controlled-frame arrival, and the
  controller-owned duration before one-shot completion.
- [x] Add a Basic-owned joint-7 POS_TOR reference-rate limiter without changing
  the six-arm-joint CONTACT_WORK endpoint semantics.
- [x] Preserve the installed gripper angle during safe-home.
- [x] Complete all software suites and configuration/manifest checks.
- [x] Run guarded safe-home and the authorized `+2 cm Z` lease-validity loop.
- [x] Return through safe-home and stop the arm software.

Detached-gripper operation remains a separate hardware-configuration task.
The current seven-motor backend cannot start when joint-7 feedback is absent;
Phase 3 safe-home now avoids moving an installed gripper but does not yet make
the entire Basic provider topology-optional.

Gate 0 physical evidence is recorded in
`PHASE3_GATE0_PHYSICAL_VALIDATION_REPORT.md`. The guarded loop found that a
fixed-duration `PRESS_MIT` one-shot can float at its deadline without reaching
the Cartesian target. The outcome is safely observable through the
`arrival_*_confirmed` fields. Phase 3 now also reports
`completion_success=false` with a distinct deadline-before-arrival outcome;
the fixed-duration physical behavior itself is unchanged.

Agent-run workspace startup now has an explicitly bounded launcher:
`platform_core/scripts/run_workspace_bounded.ps1`. It avoids the nested
PowerShell ownership problem, refuses unattended arm auto-start, and applies
an internal deadline to each health gate. The original `run_workspace.ps1`
entrypoint now delegates to the same implementation. Core-only launch,
occupied-port refusal, default Manager shutdown, PID cleanup, and port cleanup
passed the bounded lifecycle regression.

## Gate 1: Manager authority and global shutdown

- [x] Add a real Manager shutdown-plan route with stable ordering, request
  identity, acknowledgement, timeout, and partial-failure state.
- [x] Keep the local authoritative arm shutdown helper as the safety fallback.
- [ ] Compare Manager decisions with local leases and motion-inhibit state in
  shadow mode.
- [ ] Change one authority check at a time from observe to enforce.
- [ ] Prove that Manager/Fabric failure cannot remove Basic gravity support or
  bypass Basic fencing.

Manager provider-sequence execution is implemented behind
`MANAGER_SHUTDOWN_EXECUTION_ENABLED`. It is asynchronous, fences new work when
accepted, requires Basic safe-state acknowledgement, and leaves
Fabric/Manager to the supervisor. The guarded Gate 1 run passed after fixing
Basic-vs-motion-provider classification. The current workspace enables this
one validated policy; the installation template remains disabled.

## Gate 2: discovery and binding

- [x] Use OpenAI Agents SDK hosted-tool style discovery: expose concise tool
  descriptors first, load detailed Skill instructions only after selection,
  and keep the selected Skill's allowed operations narrow.
- [x] Keep prompt constraints during initial tests so the agent can select only
  the reviewed Skill and cannot invent a physical operation.
- [x] Prefer capability binding through Manager.
- [x] Retain explicit provider ID as deterministic fallback and record which path
  was selected.
- [x] Make missing, ambiguous, stale, and incompatible bindings visible to both
  the agent and the development UI.

Manager creation and GET now revalidate provider instance, boot, readiness,
health, and residency. The Test Agent development UI surfaces current, stale,
unresolved, and fallback validity plus validation issues. Binding remains
advisory while these states are observed.

## Gate 3: direct control and transparent audit

- [x] Keep controller calls direct so Fabric latency is outside the motion path.
- [x] Complete the provider-local copy before submitting to the controlled target.
- [x] Record the exact request, canonical hash, binding, authority, lease
  generation, target identity, and accepted/rejected lifecycle.
- [x] Publish Fabric copies asynchronously and replay from the local outbox.
- [x] Add a strict mode only after best-effort mode has no unexplained Fabric
  delivery gaps and the remaining lifecycle gap has a corrected, tested cause.

Strict pre-action persistence is enabled in this workspace after software
failure-path tests and a guarded `+0.03 m Z` physical run. The installation
template remains shadow. Post-action audit failure is reported without
rewriting an already completed target outcome.

## Gate 4: controller-owned motion policy

- [x] Keep singularity avoidance, speed choice, waypoint continuity, collision
  policy, endpoint residuals, and arrival confirmation in Integrated.
- [x] Compare vegetable cutting's legacy interpolation with controller plans
  without using the cutting prototype as the policy owner.
- [ ] Move callers from legacy local interpolation to controller-owned planning
  one caller at a time.
- [ ] Enforce preview freshness and semantic-scene freshness only after each
  caller can surface a useful rejection reason and recovery path.

Slice-cutting behavior remains future work. It will own the slicing intent and
axial/sawing pattern, while Integrated owns safe execution quality.

## Gate 5: RGB-D and spatial registration

- [x] Keep Orbbec shared-memory RGB-D and its direct branded fallback.
- [x] Keep Fabric limited to timestamps, shared-memory references, channel
  geometry, alignment metadata, and small numeric/text fields.
- [x] Preserve independent RGB, IR, native-depth, and registered-depth resolution,
  aspect ratio, boundary, valid region, and timestamp metadata.
- [x] Allow an Orbbec provider to write custom alignment into the generic
  descriptor.
- [ ] Migrate consumers to the generic shared-memory route individually.
- [x] Retain the direct Orbbec route as a published fallback after generic-route
  enforcement.

The provider now publishes one atomic route-set observation containing both
routes. Consumer manifests no longer make the generic route mandatory; they
declare an execution-time generic-first/direct-fallback policy.

## Gate 6: finite perception and registration Skills

- [x] Make stationary workcell calibration the normal finite base/world
  registration workflow.
- [ ] Keep FoundationPose Skill-local and finite; retire the resident provider only
  after route comparison and migration.
- [x] Keep `spatial.registration.rgbd` outside Fabric as an independently bound
  operation.
- [x] Make general VLM analysis own backend selection and ordered expensive
  fallback.
- [x] Make tool-to-control-frame registration consume VLM landmarks plus spatial
  registration and require explicit review before publishing a motion-usable
  frame.
- [x] Keep the short-term Orbbec-specific shared-memory reader where required,
  while providing a generic route beside it.

VLM voting, quality-control ensembles, and multi-model consensus are a
far-future guarded-deployment item.

## Gate 7: browser development and authorization UI

- [ ] Keep every GUI browser-based.
- [x] Use neutral dark white/grey/black for ordinary chrome; reserve color for
  warnings, status meaning, images, coordinate axes, and data series.
- [x] Separate development/debug controls from observation and authorization.
- [x] Allow Skill/provider-specific authorization popups with the exact decision
  context.
- [x] Ensure approval records authorization only; it must never execute motion by
  itself.
- [ ] Create a shared theme package after behavior boundaries are stable.

The legacy FoundationPose native development window is the remaining
non-browser compatibility UI.

## Gate 8: replacement Test Agent

- [x] Build the example around a robotic objective, not a human-readable plan.
- [ ] Observe the pointed object, choose a reviewed Skill through discovery,
  propose a front or top end-effector pose, request controller preview, and ask
  permission only when the safety policy requires it.
- [x] Keep physical execution disabled until binding, audit, authorization, and
  controller enforcement have passed separately.
- [ ] Add physical movement only as a late progressive-enforcement step.

The nonphysical endpoint now calls Integrated's real controller-owned transit
preview with a hard deadline. It creates authorization only after a valid
`SHADOW_NONPHYSICAL` result that confirms control state and lease were
unchanged. The complete multi-Skill agent pipeline remains outside the initial
allowlist.

## Enforcement order

For every gate:

1. implement the generic path without making it mandatory;
2. connect one producer and one consumer while retaining the fallback;
3. test the path independently in software;
4. observe it in shadow beside the existing route;
5. enable enforcement for one interconnection;
6. run regression and, where applicable, a user-attended guarded test;
7. only then remove or retire the replaced path.

No single Phase 3 switch may simultaneously change discovery, binding,
authority, planning, lease ownership, and physical execution.
