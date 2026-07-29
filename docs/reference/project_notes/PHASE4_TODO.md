# Phase 4 Controlled Adoption and End-to-End Agent Operation

Date: 2026-07-27
Status: checkpoint closed with explicit Phase 5 carryovers. Strict controller
audit remains enforced after guarded physical validation. Unfinished
checkboxes are not accepted as complete; binding, Manager authority, and
generic-route work continues in Phase 5, while Agent SDK physical execution
remains disabled.

## Readiness decision

The project is in Phase 4 development. Phase 3 established the required
mechanisms, the current implementation has a 444-test baseline, and the project
has physically validated
the Manager-owned shutdown path. The remaining Phase 3 items are controlled
migration, observation, and enforcement work, so they become explicit Phase 4
gates rather than hidden prerequisites.

This does not mean the system is ready for unrestricted physical-agent
execution. Phase 4 must first connect and validate one complete nonphysical
agent loop, observe each policy boundary in shadow, and enforce one boundary at
a time. Every physical gate requires a new user-approved motion envelope; prior
test authorization does not carry forward.

## Phase 4 objective

Connect the finite Skills, provider bindings, generic RGB-D route, world-frame
registration, Integrated-owned planning, authorization, audit, and Manager
authority into one observable robotic-agent loop:

1. receive a robotic objective involving a pointed object;
2. discover and select a reviewed Skill through the OpenAI Agents SDK;
3. bind the required providers with explicit fallback identities;
4. acquire and interpret RGB-D data;
5. register the observation into the stationary workcell frame;
6. propose a front or top end-effector observation pose;
7. obtain a real nonphysical Integrated controller preview;
8. request authorization only when the active safety policy requires it;
9. preserve an exact local record of every request submitted to a controlled
   target; and
10. execute only after the relevant authority, freshness, audit, and
    authorization gates have been separately enforced and validated.

The agent should primarily act, observe, and request permission. It should not
generate a human-facing motion plan as its main output.

## Phase 3 carryover map

| Phase 3 compatibility seam | Phase 4 destination |
| --- | --- |
| Manager authority remains advisory outside shutdown | Gate 4 and Gate 9 |
| Capability binding remains advisory | Gate 1 and Gate 9 |
| Direct control audit remains best effort | Gate 3 and Gate 9 |
| Legacy callers still own some local interpolation | Gate 3 |
| Some consumers still use the branded RGB-D route | Gate 2 |
| FoundationPose remains a resident compatibility provider | Gate 7 |
| FoundationPose retains a native development UI | Gate 6 and Gate 7 |
| Test Agent stops after preview and authorization recording | Gate 5 and Gate 8 |

## Gate 0: reproducible Phase 4 baseline

- [x] Accept the current 444-test matrix as the Phase 4 regression floor.
- [x] Keep the frozen pre-housecleaning copy completely outside runtime imports,
  manifests, provider configuration, and discovery paths.
- [x] Add independent feature flags for binding enforcement, strict controller
  audit, authority enforcement, generic-route enforcement, and physical
  execution. All new flags start disabled.
- [x] Add one machine-readable policy-status endpoint or status document that
  shows which boundaries are `SHADOW`, `ENFORCED`, or `FALLBACK`.
- [ ] Build deterministic recording and replay for RGB-D route descriptors,
  referenced sensor payloads, Fabric metadata, transforms, bindings, controller
  previews, leases, authorization decisions, and audit events.
- [ ] Ensure replay never opens physical providers and cannot submit a physical
  controller action.
- [ ] Add bounded scenario runners for success, stale data, recycled shared
  memory, provider restart, lease loss, audit failure, and Manager/Fabric loss.

Gate 0 exit: the current behavior can be reproduced without hardware, and each
future enforcement switch can be tested independently.

## Gate 1: executable finite-Skill adapters

- [ ] Add explicit, allowlisted Agent SDK adapters for
  `calibrate-stationary-workcell`, general VLM analysis,
  `spatial.registration.rgbd`, and `register-tool-to-control-frame`.
- [x] Keep concise discovery descriptors separate from detailed Skill
  instructions and load the latter only after model selection.
- [x] Give every currently enabled adapter typed inputs, validated results, a hard deadline,
  cancellation behavior, provenance, and a bounded set of provider operations.
- [ ] Bind capabilities through Manager first and preserve an explicit provider
  ID as the deterministic fallback.
- [ ] Revalidate the selected provider instance, boot identity, readiness,
  health, residency, and capability immediately before each consequential use.
- [ ] Prevent a stale or ambiguous binding from silently switching to a
  different provider.
- [x] Keep the local vegetable-cutting prototype undiscoverable.
- [x] Do not add the future slice-cutting Skill in this phase gate.

Gate 1 exit: the agent can select and run each reviewed finite Skill in replay
or nonphysical mode without arbitrary code execution.

## Gate 2: flexible generic RGB-D consumption

- [x] Define one consumer interface that independently describes RGB, IR,
  native depth, registered depth, channel resolution, aspect ratio, crop or
  valid boundary, intrinsics, extrinsics, clock domain, and alignment revision.
- [x] Continue carrying large image/depth payloads through shared memory.
  Fabric carries timestamps, BufferRefs, alignment metadata, and small
  numeric/text state only.
- [x] Allow the Orbbec provider to publish its custom mismatched-channel
  alignment through the generic descriptor.
- [x] Keep the direct Orbbec shared-memory route in the same atomic Fabric route
  set as a fallback.
- [ ] Migrate `spatial.registration.rgbd` first, then stationary calibration,
  tool registration, and other consumers one at a time.
- [x] Record which route each operation selected and why.
- [ ] Compare generic and direct results on the same recorded frames.
- [x] Test slot-generation changes, recycled BufferRefs, mismatched timestamps,
  missing channels, partial valid regions, and non-Orbbec camera descriptors.
- [ ] Enable generic-route preference for only one consumer after the direct
  fallback has been exercised successfully.

Gate 2 exit: one production consumer uses the generic route by default while
the branded route remains a tested fallback.

## Gate 3: controller-owned planning and exact submission audit

- [ ] Inventory every Skill and UI that still computes physical interpolation,
  speed, singularity avoidance, collision behavior, or arrival timing.
- [ ] Replace one caller at a time with Integrated preview and commit requests.
- [ ] Keep task intent, such as front/top observation or future slicing, in the
  Skill while Integrated owns safe path quality and execution timing.
- [ ] Require preview identity, target identity, semantic-scene revision, world
  transform revision, and expiry on commit.
- [ ] Reject stale preview or scene state with a recovery reason that the agent
  can act on.
- [x] Preserve the exact provider-local `SUBMITTED` record before the target
  call and asynchronously mirror it to Fabric.
- [x] Show target outcome and post-action audit outcome separately.
- [x] Add audit-gap metrics, bounded replay, and a browser-visible request
  timeline.
- [x] Soak best-effort audit before enabling strict pre-action persistence for
  one non-motion endpoint.
- [x] Enable strict audit for a physical endpoint only after its failure path
  has been tested without removing Basic support.

Gate 3 exit: general motion quality has one owner, and every controlled target
submission has a locally durable, inspectable copy.

## Gate 4: authority, lease, and relinquishment state machine

- [ ] Specify one shared state machine for Manager authority, Integrated local
  authority, Basic fencing, motion inhibit, lease generation, and provider
  residency.
- [x] Preserve the current safe baseline: an error defaults to gravity float.
- [ ] For healthy operation without an error, retain the current control mode
  until the next valid command rather than introducing a timer-driven drop or
  mode change.
- [ ] Keep safe-home independent of gripper width/angle and tolerate a
  configured detached gripper without generating a gripper command.
- [ ] Compare Manager authority decisions with local lease and motion-inhibit
  decisions in shadow and record every disagreement.
- [ ] Test lease expiry, stale fencing generation, preemption, provider restart,
  Manager restart, Fabric loss, Integrated loss, upstream-client loss, and
  duplicate command delivery in simulation/replay.
- [ ] Prove that Manager or Fabric failure cannot remove Basic gravity support
  or bypass Basic fencing.
- [x] Keep Manager-owned global shutdown as the normal path and the local
  authoritative helper as the explicit fallback.
- [ ] Enforce only one authority transition after its shadow disagreement count
  is understood.

Gate 4 exit: authority behavior has no conflicting command source, and every
loss/error transition has one deterministic safe outcome.

## Gate 5: complete nonphysical Test Agent loop

- [ ] Connect pointed-object observation to reviewed Skill discovery instead of
  a hard-coded pipeline.
- [ ] Bind the camera, workcell registration, VLM, spatial registration,
  tool/object registration, and Integrated preview capabilities.
- [ ] Capture RGB-D, identify the selected landmark/object, and recover its 3D
  workcell position with provenance and uncertainty.
- [ ] Propose a front or top end-effector pose based on the robotic objective
  and current scene, not a human-readable plan.
- [x] Request a real `SHADOW_NONPHYSICAL` Integrated preview.
- [ ] Create authorization only from a current, accepted preview.
- [ ] Ensure approval records a decision but never executes the action.
- [ ] Re-observe or replan when binding, sensor data, transform, scene, or
  preview becomes stale.
- [ ] Exercise the full loop against deterministic replay before live sensors.
- [x] Ensure no execution tool is exposed to the Agent SDK during this gate.

Gate 5 exit: a model-selected, end-to-end agent loop reaches an inspectable
authorization decision without any physical execution path.

## Gate 6: unified browser observation, development, and authorization UI

- [ ] Create a shared neutral dark theme package using white/grey/black for
  ordinary chrome and color only for warnings, meaningful state, imagery,
  coordinate axes, and data series.
- [ ] Keep all new Skill and provider UIs browser-based.
- [ ] Separate ordinary observation, development overrides, and authorization
  into visibly distinct surfaces and permissions.
- [ ] Provide Skill/provider-specific authorization popups containing target,
  motion envelope, preview age, binding, authority owner, lease generation,
  scene/transform revisions, safety reason, and expiry.
- [ ] Add authenticated decision identity and reject expired or duplicate
  decisions.
- [x] Show exact controller submissions, outcomes, audit state, binding state,
  and fallback route use in the development UI.
- [ ] Move the remaining FoundationPose development workflow to a browser UI or
  retire it with the resident provider.
- [ ] Keep development overrides unavailable to the ordinary Agent SDK.

Gate 6 exit: every maintained GUI is browser-based and uses the shared theme,
while development authority remains separate from operator authorization.

## Gate 7: stationary calibration and FoundationPose retirement

- [ ] Build a recorded comparison set covering robot base, gripper-equipped
  effector, detached-gripper effector, lighting variation, partial occlusion,
  and camera/workcell restart.
- [ ] Compare the stationary calibration Skill with the current alignment and
  resident FoundationPose routes for accuracy, repeatability, runtime, failure
  clarity, and operator effort.
- [ ] Invoke FoundationPose only as a finite Skill-local operation during the
  migration; do not create a new always-resident dependency.
- [ ] Publish world/base/control transforms with timestamp, calibration
  revision, method, confidence, covariance or error estimate, and source
  observation provenance.
- [ ] Require explicit review before a candidate calibration becomes
  motion-usable.
- [ ] Verify that gripper absence does not make base/workcell registration
  impossible or create gripper-controller errors.
- [ ] Make stationary calibration the normal route after the acceptance
  thresholds pass.
- [ ] Retire the resident FoundationPose provider and legacy alignment Skill
  only after recorded and guarded live fallback tests pass.

Gate 7 exit: normal stationary registration is finite, discoverable, reviewed,
and independent of a resident FoundationPose process.

## Gate 8: guarded physical agent validation

- [x] Obtain a new explicit user-approved motion, rotation, gripper, padding,
  and provider/mode-switching envelope for each physical round.
- [ ] Start with bounded launcher and provider health checks; start no arm
  provider implicitly.
- [x] Establish Basic support and perform the agreed safe-home step before the
  first authorized motion unless the reviewed test explicitly requires another
  safe starting state.
- [ ] Validate binding, audit, authorization, and Integrated preview separately
  before enabling the corresponding execution adapter.
- [ ] Test one failure mode per round and never combine a new authority rule,
  lease transition, controller mode, planner policy, and agent behavior in the
  same first physical trial.
- [x] Verify error-to-gravity-float behavior and healthy control-mode continuity
  under the approved safe envelope.
- [ ] Verify configured detached-gripper behavior. Installed-gripper
  preservation passed this round.
- [ ] Run one bounded front/top observation move only after the nonphysical
  replay loop and all prerequisite enforcement gates pass.
- [ ] Before safe-home or shutdown, notify the user when padding or workspace
  preparation must change.
- [x] End every round in an explicitly observed safe state and stop the arm
  software through the validated Manager path or its local fallback.

Gate 8 exit: one agent-selected observation move completes inside a reviewed
envelope, with correct authority, audit, authorization, fallback, and shutdown
evidence.

## Gate 9: progressive enforcement and compatibility retirement

- [ ] Enable capability-binding enforcement for one reviewed Skill.
- [ ] Enable generic RGB-D route enforcement for one migrated consumer.
- [x] Enable strict local audit for one reviewed controlled endpoint.
- [ ] Enable Manager authority enforcement for one reviewed transition.
- [ ] Keep separate rollback flags and explicit fallback telemetry for each
  boundary.
- [x] Run the full software matrix after the strict-audit enforcement change.
- [x] Run a user-attended guarded physical regression for the strict-audit
  enforcement change.
- [ ] Remove a fallback only after its replacement passes replay, shadow,
  enforcement, restart, stale-data, and guarded physical tests.
- [ ] Update package manifests, API contracts, operator documentation, and
  validation reports after each retirement.

Gate 9 exit: normal operation uses the new interconnections without relying on
hidden legacy paths, while every remaining fallback is deliberate and visible.

## Explicitly deferred work

- [ ] Slice-cutting behavior and axial/sawing motion generation.
- [ ] VLM voting, quality-control ensembles, and multi-model consensus.
- [ ] General autonomous manipulation outside the reviewed stationary
  workcell.
- [ ] Broad multi-robot or mobile-base authority.
- [ ] Removal of the direct Orbbec route before a generic non-Orbbec producer
  and fallback test exist.
- [ ] Production VIO backend selection among the Python ESKF, Basalt, and
  OpenVINS/MSCKF. Deterministic recording/replay is included in Phase 4, but
  backend replacement remains a separate sensory-localization track.

## Phase 4 exit criteria

Phase 4 is complete only when:

- [ ] the complete selected-Skill agent loop passes deterministic replay and
  live-sensor nonphysical validation;
- [ ] one consumer normally uses the generic RGB-D route with a tested direct
  fallback;
- [ ] one controller caller has no local path-planning ownership;
- [ ] one binding, one audit boundary, and one authority transition are
  independently enforced and reversible;
- [ ] stationary workcell calibration is the normal reviewed route;
- [ ] every maintained GUI is browser-based and shares the neutral dark theme;
- [ ] failure injection proves error-to-gravity-float behavior, healthy
  control-mode continuity, and preservation of Basic support;
- [ ] one user-attended front/top observation move passes end to end;
- [ ] Manager shutdown and the local fallback both retain their validated
  safety behavior; and
- [ ] regression, replay, physical evidence, API documentation, and package
  manifests agree.

## Required implementation order

1. establish replay and independent feature flags;
2. add finite-Skill adapters and generic RGB-D consumers without enforcement;
3. complete the end-to-end nonphysical agent loop;
4. finish browser observability and authorization identity;
5. observe binding, audit, authority, and route decisions in shadow;
6. enforce exactly one boundary;
7. run regression and the applicable guarded validation;
8. repeat for the next boundary;
9. perform the final bounded agent observation move; and
10. retire only the compatibility paths whose replacements have passed every
    gate.

No Phase 4 change may simultaneously introduce a new agent execution adapter,
new physical authority rule, new controller mode, and new motion behavior.
