# Phase 5 Stationary-Workcell Agent Integration and Controlled Adoption

Date: 2026-07-29
Status: active. The narrow real Agent SDK no-contact checkpoint and final safe
shutdown passed. The compound pointed-object Agent Skill, general Cartesian
direction semantics, and remaining enforcement/retirement work are incomplete.

## Readiness decision

Phase 5 is approved and active. Gate 0 is complete. The replay core and the
first read-only spatial-registration adapter are implemented. Spatial binding
and its generic RGB-D route are enforced independently; physical execution
remains disabled pending its listed evidence gates.

Phase 4 has established a 444-test floor, independent policy flags, bounded
operation deadlines, a flexible generic RGB-D descriptor, actual VLM-reviewed
RGB-D alignment, exact controller submission audit, and one guarded strict-audit
physical validation. Manager-owned shutdown also remains validated.

Phase 4 did not complete all of its original exit criteria. Phase 5 therefore
accepts the unfinished work below as explicit carryover rather than silently
declaring it complete:

- deterministic whole-loop recording and replay;
- the complete set of finite-Skill Agent SDK adapters;
- one enforced capability binding;
- one enforced generic RGB-D consumer route;
- one controller caller fully migrated away from local motion planning;
- one enforced Manager authority transition;
- the complete pointed-object-to-preview agent loop;
- stationary-calibration acceptance and resident FoundationPose retirement;
- shared browser UI and decision identity; and
- one agent-selected front/top observation move.

Phase 4 is relabelled `checkpoint closed with Phase 5 carryovers`. Its
strict-audit result remains accepted and enabled.

## Phase 5 objective

Produce one bounded stationary-workcell robotic agent that can:

1. receive a robotic objective concerning a pointed object;
2. discover and select only reviewed finite Skills;
3. bind current provider instances through Manager, with explicit provider-ID
   fallback where required;
4. validate real RGB-D content and register it into a reviewed workcell frame;
5. identify the object or landmark and recover a metric 3D position with
   uncertainty and provenance;
6. propose a front or top end-effector observation pose;
7. ask Integrated for a controller-owned preview;
8. request decision-specific authorization only when the active safety policy
   requires it;
9. preserve exact controller and authority records; and
10. after separate enforcement gates pass and a new physical envelope is
    approved, perform one bounded observation move.

The agent should act, observe, and request authorization. Its primary output is
not a human-readable motion plan. Skills own task intent; Integrated owns path
quality, singularity avoidance, collision policy, speed, timing, and arrival
classification.

## Gate 0: close the phase boundary and freeze the baseline

- [x] Approve this Phase 5 scope and the Phase 4 carryover list.
- [x] Change Phase 4 status to `checkpoint closed with Phase 5 carryovers`
  without marking unfinished checkboxes complete.
- [x] Preserve strict controller audit as the only newly enforced Phase 4
  boundary.
- [x] Record the exact Phase 5 starting policy state:
  - binding `SHADOW`;
  - controller audit `ENFORCED`;
  - Manager authority `SHADOW`;
  - generic RGB-D route `SHADOW`; and
  - Agent SDK physical execution `DISABLED`.
- [x] Re-run and accept the 444-test matrix as the minimum Phase 5 floor.
- [x] Verify that no runtime import, manifest, provider configuration, Skill
  discovery path, or test path resolves into the frozen pre-housecleaning copy.
- [ ] Give every Phase 5 command runner a hard deadline, useful progress
  heartbeat, idle deadline, and bounded cleanup path.
- [x] Keep arm providers opt-in; no unattended workspace launcher may auto-start
  Basic or Integrated.

Gate 0 exit: the new phase starts from a reproducible, stopped, nonphysical
baseline with no ambiguous carryover state.

## Gate 1: deterministic whole-loop recording and replay

- [ ] Define a versioned capture bundle containing:
  - generic and direct RGB-D route descriptors;
  - immutable copies of referenced RGB, IR, native-depth, and registered-depth
    payloads;
  - BufferRef mapping name, slot, generation, size, format, stride, and
    timestamps;
  - custom alignment metadata, valid boundaries, intrinsics, and extrinsics;
  - Fabric observations and revisions;
  - Manager bindings and provider instance/boot identity;
  - workcell transforms and calibration evidence;
  - VLM requests, structured results, model/backend provenance, and image
    hashes;
  - controller previews, lease state, authorization decisions, and exact audit
    events.
- [x] Copy shared-memory payloads into the capture while the referenced
  generation is still current; reject recycled or changing slots.
- [x] Remap captured payloads into replay-only shared memory rather than
  teaching consumers a special byte-array path.
- [x] Make replay use a separate namespace and explicit `REPLAY` provenance.
- [x] Make replay structurally incapable of starting hardware providers,
  acquiring a physical lease, calling a physical controller endpoint, or
  enabling the Agent SDK execution adapter.
- [x] Add deterministic scenarios for:
  - success;
  - stale RGB or depth;
  - independent channel frame rates;
  - mismatched resolutions, aspect ratios, and boundaries;
  - recycled BufferRefs;
  - alignment-revision changes;
  - provider restart and boot-ID change;
  - VIO epoch change;
  - stale world transform;
  - rejected controller preview;
  - lease expiry or stale fencing generation;
  - audit persistence failure;
  - Manager loss; and
  - Fabric loss.
- [x] Add capture validation, redaction, size limits, retention rules, and a
  browser-visible provenance summary.

Gate 1 exit: the entire nonphysical agent loop and every enforcement failure
path can be reproduced without hardware or paid model calls.

## Gate 2: finite-Skill adapters and enforced binding

- [x] Add explicit allowlisted Agent SDK adapters for:
  - `calibrate-stationary-workcell`;
  - general VLM scene/landmark analysis;
  - `spatial.registration.rgbd`; and
  - `register-tool-to-control-frame`.
- [x] Keep concise Skill discovery descriptors separate from detailed
  instructions; load full instructions only after model selection.
- [ ] Give every adapter a closed JSON schema, typed result, bounded provider
  operation list, hard/idle deadlines, cancellation, and provenance.
- [x] Keep the vegetable-cutting prototype outside ordinary discovery.
- [x] Do not expose arbitrary subprocess, file-write, network, provider-start,
  controller-execution, or development-override tools to the model.
- [x] Bind required capabilities through Manager first.
- [x] Preserve an explicit provider ID as a deterministic, visible fallback;
  never silently substitute a provider.
- [ ] Immediately before consequential use, revalidate:
  - binding ID;
  - provider ID and instance ID;
  - boot ID;
  - readiness and health;
  - residency;
  - capability version; and
  - source time and association under the consuming Skill's temporal policy.
- [x] Reject missing, ambiguous, stale, restarted, or incompatible bindings with
  a recovery reason the agent can act on.
- [x] Observe shadow binding decisions in replay and live nonphysical runs.
- [x] Enforce binding for one read-only Skill first.
- [x] Exercise explicit provider-ID fallback after enforcement and record why
  it was selected. A cold explicit fallback is rejected until separately
  activated; the same ready/HOT provider then resolves as a current advertised
  capability with exact provider, instance, boot, and configured-fallback
  provenance.
- [ ] Keep an independent rollback flag and binding/fallback metrics. The
  independent spatial binding flag exists; durable aggregate metrics remain.

Gate 2 exit: one reviewed finite Skill uses enforced current-instance binding,
while explicit fallback remains tested and visible.

## Gate 3: production generic RGB-D route

- [x] Migrate `spatial.registration.rgbd` to the generic route first.
- [x] Require independent RGB, IR, native-depth, and registered-depth:
  resolution, aspect ratio, stride, boundary, valid region, timestamp, cadence,
  intrinsics, extrinsics, and alignment revision. The current descriptor
  validator covers geometry, stride, boundary, valid region, timestamp policy,
  calibration references, synchronization, and custom alignment metadata;
  live cadence remains checked by RGB-D QC.
- [x] Keep large payloads in shared memory. Fabric carries only timestamps,
  BufferRefs, alignment metadata, revisions, and small numeric/text state.
- [x] Continue publishing the direct Orbbec shared-memory route in the same
  atomic route set as a fallback.
- [x] Let the Orbbec producer write its custom crop/alignment and
  channel-boundary relationship into the generic descriptor.
- [x] Compare generic and direct spatial-registration results on the same
  captured frames.
- [x] Validate content with numerical checks and VLM review of RGB,
  registered-depth, and overlay images; image availability alone is not a pass.
  The acceptance review uses Codex's direct multimodal inspection; external
  model/API review is used only when explicitly requested.
- [x] Test stale channels, different frame rates, partial valid regions,
  alignment changes, slot recycling, and registered-depth holes. Coverage is
  split across cadence validation, enforced spatial registration, immutable
  replay, and depth-neighborhood selection tests.
- [x] Add at least one synthetic or replayed non-Orbbec producer descriptor.
  The synthetic descriptor exercises four different channel grids and a
  cropped provider-custom registered-depth boundary. A live brand-neutral
  shared-memory reader remains future work; provider-supplied allowlisted
  transport adapters are still permitted.
- [x] Enable generic-route enforcement for `spatial.registration.rgbd` only
  after the direct fallback passes.
- [ ] Migrate stationary calibration and tool registration one at a time after
  the first consumer is stable.
- [x] Add the generic-first `locate-effector-front` read-only consumer using
  native registered-depth-grid VLM coordinates, provider valid-region
  enforcement, exact-depth validation, and post-VLM camera revalidation. Keep
  it outside the active Agent allowlist pending recorded and live nonphysical
  review.
- [ ] Keep route choice, fallback reason, channel timing, boundary validation,
  and VLM QC visible in the browser UI.

Gate 3 exit: one production consumer is generic-first under enforcement, with
an exercised direct Orbbec fallback and no copy of large frames in Fabric.

## Gate 4: stationary calibration and FoundationPose retirement

- [ ] Build a recorded comparison set containing:
  - robot base and effector observations;
  - installed-gripper and configured detached-gripper cases;
  - lighting variation;
  - reflective geometry;
  - partial occlusion;
  - camera restart;
  - VIO/workcell epoch change; and
  - rejected first attempts.
  The versioned route-run and comparison schemas now exist; matching recorded
  results for both routes are still required.
- [ ] Compare `PROVIDER_COMPATIBILITY` and `SKILL_LOCAL` routes on identical
  observations for latency, accuracy, repeatability, failure clarity, GPU
  release, and operator effort. The fail-closed comparison evaluator is
  implemented and tested. The live Phase 5 RGB-D replay bundle now produces
  one byte-verified common observation fingerprint, but no matching estimator
  result pair exists yet.
- [x] Treat FoundationPose as a finite estimator operation owned by the
  calibration Skill, not as a continuously resident tracking Provider.
  `SKILL_LOCAL` now creates and closes its backend per finite attempt; cleanup
  failure rejects the result. The Provider route remains an explicit
  compatibility path.
- [x] Publish candidate world/base/control transforms with:
  - timestamp and expiry;
  - workcell/calibration revision;
  - method and estimator version;
  - confidence;
  - covariance or bounded error estimate;
  - camera route and source BufferRefs;
  - VIO epoch; and
  - review state.
- [x] Require explicit review before a candidate transform becomes
  motion-usable. Candidate records are non-motion-usable, `ENFORCED` mode
  withholds legacy transform streams, and enforced `AUTO` rejects pending or
  expired prior candidates. Append-only, idempotent approve/reject decisions
  require a decision-scoped externally verified identity and remain
  `NOT_ACTIVATED` until Manager verifies the exact digest and current
  camera/VIO identity. Fabric excludes pending/expired/non-motion transforms
  from graph queries, and the local cutting consumer rejects them before
  planning. Live approve, activate, use, reject, and revoke paths passed.
- [ ] Reject calibration if the workcell/VIO epoch changes during estimation.
- [ ] Verify that a detached gripper does not block base registration or cause a
  gripper command/error.
- [ ] Make stationary calibration the normal finite route only after recorded
  and guarded-live thresholds pass.
- [ ] Move or retire the remaining FoundationPose native development UI before
  removing the resident provider.
- [ ] Remove resident FoundationPose discovery/configuration only after the
  finite route and explicit compatibility fallback pass restart and failure
  tests.

Gate 4 exit: normal workcell registration is finite, reviewed, revisioned, and
does not require a resident FoundationPose process.

## Gate 5: controller-owned motion contract

- [ ] Inventory every maintained Skill, provider, test agent, and UI that still
  computes interpolation, waypoint timing, speed, singularity avoidance,
  collision behavior, or arrival timing.
- [ ] Classify each calculation as task intent, controller policy, visualization,
  or legacy compatibility code.
- [x] Select the Test Agent front/top observation caller for the first complete
  migration.
- [x] Let the caller submit only semantic observation intent, target pose, and
  target constraints.
- [x] Require Integrated preview identity and bind it to:
  - normalized target identity;
  - semantic-scene revision;
  - world-transform revision;
  - binding/provider boot identity;
  - lease/fencing generation;
  - controller configuration revision; and
  - expiry.
- [x] Require authorization to reference that exact preview identity and motion
  envelope.
- [ ] Reject stale preview, changed scene/transform, changed provider boot,
  changed lease generation, duplicate commit, or expired authorization.
- [ ] Return structured recovery reasons so the agent can re-observe, rebind,
  re-preview, or request authorization again.
- [x] Keep exact provider-local audit before every controlled request and
  asynchronous Fabric copying after local persistence.
- [ ] Show controlled outcome, arrival classification, and post-action audit
  outcome as separate fields.
- [ ] Remove the selected caller's legacy planning only after replay, shadow,
  strict audit, stale-state, and guarded physical tests pass.

Gate 5 exit: one ordinary caller has no ownership of general motion quality;
Integrated is its sole path-planning and execution-policy owner.

## Gate 6: authority, lease, and relinquishment

- [ ] Define one versioned state machine covering:
  - Manager authority;
  - Integrated residency and local control state;
  - Basic operational lease and fencing generation;
  - motion inhibit;
  - authorization state;
  - upstream-client ownership; and
  - relinquishment/shutdown.
  - Progress: the versioned shadow evaluator now separates Manager task
    authority, Integrated operational-writer activity, and the Basic residency
    lease, including distinct fencing namespaces, standby, motion inhibit,
    authorization and relinquishment context, and explicit lineage. Upstream
    lineage is not yet supplied at the active action boundary, so the complete
    state machine remains open. The accepted route is recorded in
    `PHASE5_AUTHORITY_LINEAGE_DECISION.md`.
- [ ] Preserve the primary error invariant: any control error defaults to
  gravity float.
- [ ] For healthy, non-error operation, keep the current control mode until the
  next valid command rather than introducing a timer-driven drop or mode
  change.
- [ ] Preserve Basic gravity support independently of Manager, Fabric,
  Integrated, Agent SDK, browser, or upstream-client availability.
- [ ] Make safe-home preserve the installed gripper angle/width and omit gripper
  output when the configured gripper is detached.
- [x] Record Manager-versus-local authority decisions in shadow and count every
  poll, transition, state, and disagreement by stable reason.
- [ ] Test in replay/simulation:
  - lease expiry and renewal loss;
  - stale fencing generation;
  - duplicate command delivery;
  - preemption;
  - Basic, Integrated, Manager, and Fabric restart/loss;
  - browser/client disconnect;
  - Agent SDK cancellation or idle timeout; and
  - shutdown during partial failure.
- [ ] Prove that Manager/Fabric failure cannot bypass Basic fencing, remove
  gravity support, or create a second command writer.
- [ ] Enforce one non-motion authority transition first.
- [ ] Guardedly validate one authority handoff only after its shadow
  disagreement count is understood.
- [ ] Keep Manager-owned global shutdown normal and the local authoritative
  helper as an explicit fallback.

Gate 6 exit: every writer, handoff, timeout, failure, and shutdown path has one
deterministic owner and one safe outcome.

## Gate 7: complete nonphysical robotic-agent loop

- [ ] Create one replay objective such as “move the effector to observe the
  pointed object from the front or top.”
- [ ] Let the model select reviewed Skills through Agent SDK discovery; do not
  hard-code the complete sequence as one hidden pipeline.
- [ ] Bind the camera, RGB-D registration, stationary workcell, VLM,
  spatial-registration, tool/object-registration, and Integrated-preview
  capabilities.
- [ ] Capture and VLM-check current RGB-D content.
- [ ] Identify the pointed object/landmark with confidence and structured
  evidence.
- [ ] Recover its 3D workcell position with uncertainty, selected depth pixel,
  valid-neighborhood statistics, route, alignment, transform, and source-frame
  provenance.
- [ ] Propose a front or top observation pose from the robotic objective and
  scene constraints.
- [ ] Obtain a real `SHADOW_NONPHYSICAL` Integrated preview.
- [x] Create authorization only from the current accepted preview.
- [x] Make approval record a decision only; it must not execute motion.
- [ ] Re-observe or replan when sensor, binding, calibration, scene, preview,
  lease, or authorization state becomes stale.
- [ ] Keep model tool concurrency at one for consequential operations.
- [x] Keep execution tools absent from the Agent SDK during this gate.
- [ ] Pass deterministic replay before using live camera data.
- [ ] Pass live-sensor nonphysical validation without starting either arm
  provider.
- [ ] Verify hard and idle deadlines by intentionally stalling one adapter and
  one VLM response.

Gate 7 exit: the model-selected loop ends at a current inspectable authorization
decision, with no physical execution path available.

## Gate 8: unified browser observation and authorization UI

- [ ] Extract a shared browser theme package for maintained Skills and
  providers.
- [ ] Use white, grey, and black for ordinary chrome; reserve color for
  warnings, meaningful state, images, coordinate axes, and data series.
- [ ] Keep every new maintained GUI browser-based.
- [ ] Separate observation, authorization, and development override surfaces
  and permissions.
- [ ] Provide Skill/provider-specific authorization popups containing:
  - requested action and reason;
  - target and motion envelope;
  - preview identity, age, and expiry;
  - binding/provider identity;
  - Manager/local authority owner;
  - lease/fencing generation;
  - scene/workcell-transform revisions;
  - expected safe fallback; and
  - exact decision scope.
- [ ] Add authenticated decision identity.
- [ ] Reject expired, duplicate, broadened, or mismatched decisions.
- [ ] Show exact controller submissions, controlled outcomes, audit state,
  binding/fallback state, route choice, VLM provenance, and stale-state reasons.
- [ ] Keep development overrides unavailable to the ordinary Agent SDK.
- [ ] Eliminate the last required native FoundationPose UI by browser migration
  or provider retirement.

Gate 8 exit: maintained workflows are browser-based and visually unified, while
observation, authorization, and development authority remain distinct.

## Gate 9: progressive enforcement

- [ ] Keep independent flags and rollback for binding, controller audit,
  Manager authority, generic RGB-D route, and Agent SDK physical execution.
- [ ] Preserve strict controller audit and rerun its failure-path tests after
  each controller API change.
- [ ] Enforce one boundary at a time in this order:
  1. read-only finite-Skill binding;
  2. generic RGB-D route for `spatial.registration.rgbd`;
  3. one non-motion Manager authority transition;
  4. one controller caller's preview/commit freshness contract; and
  5. the narrowly scoped physical observation adapter.
- [ ] After each switch:
  - run the full software matrix;
  - run deterministic replay;
  - inject its rollback/failure path;
  - inspect metrics and browser state;
  - return to a known policy state; and
  - update contracts, manifests, and validation evidence.
- [ ] Require a separately approved, user-attended physical regression for any
  enforcement change that can affect arm output, mode, lease, or support.
- [ ] Never combine a new authority rule, controller mode, planner behavior,
  agent tool, and physical motion in the same first trial.
- [ ] Remove no fallback in this gate unless its replacement has passed replay,
  restart, stale-data, enforcement, rollback, and guarded physical validation.

Gate 9 exit: binding, generic RGB-D, audit, and one Manager authority transition
are independently enforced, reversible, and observable.

## Gate 10: guarded front/top observation move

- [x] Obtain a new explicit authorization for:
  - translation envelope;
  - rotation envelope;
  - gripper behavior;
  - padding/workspace preparation;
  - allowed provider starts;
  - mode and lease transitions;
  - permitted VLM call budget; and
  - safe-home/shutdown sequence.
- [x] Do not inherit any Phase 2, Phase 3, or Phase 4 physical authorization.
- [x] Start the bounded core first and verify no arm provider was implicitly
  started.
- [x] Start Basic explicitly, verify hardware health, and establish the reviewed
  safe starting state.
- [x] Perform safe-home first unless the reviewed trial explicitly requires a
  different safe state.
- [x] Start Integrated and separately validate:
  - binding;
  - authority;
  - lease/fencing;
  - workcell transform;
  - RGB-D freshness and VLM QC;
  - scene revision;
  - controller preview;
  - authorization identity;
  - exact audit persistence; and
  - idle/hard deadlines.
- [x] Expose only the reviewed observation-execution adapter to the Agent SDK.
- [x] Execute one agent-selected front or top observation pose inside the exact
  approved envelope.
- [x] Do not include grasping, contact, tool registration activation, slicing,
  or vegetable cutting in this first loop.
- [x] Poll the measured trajectory with an internal deadline and preserve the
  real arrival/failure classification.
- [x] On any control error, default to gravity float.
- [x] On healthy completion, retain the reviewed control mode until the next
  valid command or explicit relinquishment.
- [x] Verify no duplicate submission, conflicting writer, lease mismatch,
  unrecorded request, unexpected gripper change, or unexpected fallback
  occurred.
- [x] Notify the user before safe-home when padding or workcell preparation must
  change.
- [x] Finish in an explicitly observed safe state.
- [x] Safe-home with gripper preservation and stop through Manager-owned
  shutdown; Manager stopped its owned Integrated process and correctly retained
  safety support at the externally owned Basic boundary, after which the
  validated local authoritative fallback completed.
- [x] Verify final physical provider and Skill UI ports are clear. Manager and
  Fabric were retained briefly for audit inspection and then stopped.

Gate 10 exit: one model-selected observation move completes or safely fails with
correct perception, registration, binding, authority, preview, authorization,
audit, idle handling, gravity support, and shutdown evidence.

## Gate 11: compatibility retirement and release evidence

- [ ] Retire the resident FoundationPose provider only if Gate 4 passed.
- [ ] Retire the selected caller's local planning only if Gate 5 passed.
- [ ] Keep the direct Orbbec route until a generic non-Orbbec producer and
  guarded fallback evidence exist.
- [ ] Keep explicit provider-ID fallback until enforced binding has sufficient
  restart and ambiguity evidence.
- [x] Keep the vegetable-cutting prototype local and undiscoverable.
- [ ] Update package manifests, versions, changelogs, contracts, operator
  instructions, browser help, and project documentation.
- [x] Produce one Phase 5 validation report separating:
  - replay evidence;
  - live-sensor nonphysical evidence;
  - enforcement evidence;
  - VLM call count and fallback use;
  - physical evidence;
  - known compatibility paths; and
  - deferred risks.
- [x] Run the complete regression matrix from a stopped workspace. The full
  local floor is `578/578` (548 Python and 30 Rust tests), including the
  local-only cutting prototype. The exact GitHub candidate passes `469/469`
  after that prototype is excluded. No provider, hardware endpoint, or VLM was
  started.
- [x] Verify the final Manager shutdown path and local fallback remain valid.
- [x] Leave all physical providers stopped and all operation registries idle.

Gate 11 exit: normal operation uses the reviewed interconnections, and every
remaining compatibility path is deliberate, documented, visible, and tested.

## Explicitly deferred beyond Phase 5

- [ ] Slice-cutting behavior, axial/sawing motion generation, and cut-progress
  recovery.
- [ ] Activating a registered blade/tool frame for physical cutting.
- [ ] Specialized action-point VLM Skills for drill tips, hammer faces, cutting
  edges, nozzles, and other task-specific geometry.
- [ ] VLM voting, quality-control ensembles, and multi-model consensus.
- [ ] General autonomous manipulation outside the reviewed stationary
  workcell.
- [ ] Grasping or contact-rich manipulation in the replacement Test Agent.
- [ ] Broad multi-robot or mobile-base authority.
- [ ] General natural-language Cartesian direction resolution and cross-frame
  axis validation. See `CARTESIAN_AXIS_ALIGNMENT_OPEN_ISSUE_20260729.md`.
- [ ] Removing the direct Orbbec fallback before a generic non-Orbbec producer
  and fallback test exist.
- [ ] Production VIO backend replacement among the Python ESKF, Basalt, and
  OpenVINS/MSCKF.

## Structured landmark decisions

- [x] Define and implement the first general structured VLM landmark:
  `locate-effector-front`. It reports the most distal valid registered-depth
  point on the rigid assembly, or two bare-gripper points whose registered 3D
  mean becomes the control reference.
- [ ] Keep specialized action-point landmark semantics on hold until each
  tool/action can be defined narrowly and without ambiguity.
- [ ] Keep the compound Agent SDK pointed-object loop on hold until its own
  structured object/pointing landmarks and whole-loop policy are resolved.
- [x] Preserve the passive Fabric boundary: Fabric reports timestamps,
  identities, association deltas, and structural validity; each Skill owns its
  temporal acceptance and post-compute continuity policy.

## Phase 5 exit criteria

Phase 5 is complete only when:

- [ ] whole-loop deterministic replay passes and is physically incapable of
  controlling hardware;
- [ ] the complete selected-Skill loop passes live-sensor nonphysical
  validation;
- [ ] one reviewed Skill uses enforced current-instance binding with tested
  explicit fallback;
- [ ] `spatial.registration.rgbd` uses the enforced generic route with tested
  direct fallback;
- [ ] stationary workcell calibration is the normal reviewed route;
- [ ] the resident FoundationPose provider is retired or has a documented,
  approved reason to remain;
- [ ] one controller caller has no local general path-planning ownership;
- [ ] one Manager authority transition is enforced and reversible;
- [ ] strict audit remains gap-free across accepted and rejected requests;
- [ ] maintained UIs are browser-based and share the neutral dark theme;
- [ ] failure injection proves error-to-gravity-float, healthy mode continuity,
  Basic support independence, and deterministic relinquishment;
- [ ] one newly authorized front/top observation move passes end to end;
- [ ] Manager shutdown and the local fallback retain validated safety behavior;
  and
- [ ] regression, replay, live-sensor, physical, API, manifest, and operator
  documentation evidence agree.

## Required implementation order

1. approve carryovers and freeze the Phase 5 baseline;
2. implement whole-loop recording/replay and failure injection;
3. complete finite-Skill adapters without physical execution;
4. enforce one read-only binding;
5. migrate and enforce one generic RGB-D consumer;
6. validate stationary calibration and begin FoundationPose retirement;
7. migrate one controller caller and implement freshness-bound commit;
8. define and shadow the shared authority state machine;
9. complete the model-selected nonphysical agent loop;
10. finish browser authorization and development separation;
11. enforce one non-motion Manager authority transition;
12. obtain new physical authorization and run one bounded observation move;
13. retire only compatibility paths whose replacements passed every gate; and
14. produce final validation and release evidence.

No Phase 5 change may simultaneously introduce a new agent execution adapter,
new physical authority rule, new controller mode, and new motion behavior.
