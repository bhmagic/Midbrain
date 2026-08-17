# Development record

## 2026-08-15 — FoundationPose recalibration loop and Agent task stop

### Report and retained evidence

The operator reported that FoundationPose appeared to run indefinitely and
that both Agent surfaces lacked a way to stop an active run without shutting
down background Providers.

The authoritative SQLite run journal retained run
`9cd2f18e-fd15-4155-b738-0b879723cca7`. It started at
`2026-08-16T02:33:27.757717Z`. The first
`calibrate_stationary_workcell` call ran from `02:33:42.938797Z` to
`02:37:51.193334Z` and completed with candidate
`20260816T023343Z-188c7353`. Activation then returned
`CANDIDATE_ORIENTATION_SUPERSEDED` and instructed the Agent to run a fresh
calibration. The Agent followed that instruction at `02:37:55.267368Z`.
The host process stopped before that second call reached a terminal journal
event, leaving the retained run marked `RUNNING` until the next journal startup
reconciliation. The configured restart reconciled it to `INTERRUPTED` at
`2026-08-16T02:52:31.464610Z`.

### Root cause

The first FoundationPose call was finite and successful. Its orientation
resolver measured `corrected_base_z_dot_world_up=0.7284993658161248`, while
the final robust accepted pose measured
`base_z_dot_world_up=0.7053930114404312`. Both measurements placed base +Z in
the correct world-up hemisphere, but candidate production required them to be
equal within `1e-6`. The producer therefore omitted semantic alignment
provenance. Activation correctly rejected the missing proof and returned a
fresh-calibration continuation, which created the apparent loop.

### FoundationPose and cancellation implementation checkpoints

1. Candidate production now validates that both the orientation-resolution
   measurement and final accepted-pose measurement are finite and world-up.
   It records the final accepted-pose value in the field consumed by
   activation and retains the earlier resolver value as a separate diagnostic.
2. The streaming-run registry now cancels every async task owned by one run.
   Cancellation propagates through the operation registry and the stationary
   calibration adapter, whose existing cleanup cancels/closes the finite
   runtime and releases its task-owned FoundationPose session.
3. `POST /api/streaming-runs/{run_id}/cancel` also discards pending prepared
   actions and approvals, publishes cancellation-requested and cancellation
   events, and terminates the journal record as `CANCELLED`.
4. Cancellation does not call Provider Manager stop operations. Camera, VIO,
   arm-control, and other background Providers remain available. A physical
   command already accepted by a controller has an explicitly unknown outcome
   and is not represented as retracted.
5. Regular and developer Agent pages now expose a disabled-by-default
   `Stop task` control while a run is active. Both surfaces consume the same
   cancellation event and retain it in shared chat history.

### Regression validation

The focused stream, API, browser-contract, and FoundationPose producer tests
passed (`65 passed`). The stationary adapter, activation, journal, operation
registry, and complete stationary-world-arm Skill tests also passed
(`128 passed, 9 subtests passed`). The complete Test Agent suite passed
(`436 passed, 27 subtests passed`). A live local browser render confirmed that
both Agent pages expose an idle-disabled `Stop task` beside `Run prompt`, with
no page-console errors. The developer diagnostics correctly reported its
expected unavailable-Fabric state while the isolated UI test server ran
without background Providers. Documentation, configuration, environment
isolation, and JSON validation passed, source integrity manifests were
refreshed, and the configured launcher then reported `health=ok` on port 8000.

### Physical test boundary

Software validation does not prove that an already submitted physical command
stopped. The next authorized hardware test should start a read-only
FoundationPose calibration, press `Stop task`, confirm a terminal `CANCELLED`
journal event, confirm task-owned FoundationPose cleanup, and separately
confirm that the background Camera/VIO/arm Providers remain available.

## 2026-08-16 — Limited Graph invocation visibility and physical children

### Invocation-visibility retained evidence

The retained run `95ce28be-1630-4638-9719-ecb2c8a208cf` emitted
`tool.search.called` for `run_limited_graph` at
`2026-08-16T08:40:05.592279Z` and completed discovery eight milliseconds later.
It emitted no `tool.called` event for `run_limited_graph`; the next actual tool
call was `configure_scene_segmentation_policy` at
`2026-08-16T08:40:08.814353Z`. The graph runner therefore received no graph.

### Limited Graph implementation checkpoints

1. Removed the separate `LIMITED_GRAPH_ALLOW_PHYSICAL_CHILDREN` environment,
   Settings, driver, and broker gate. A physical child is now included whenever
   its descriptor and FunctionTool are eligible on the active Agent route.
2. Retained exact child authorization, dynamic enablement, active-route
   intersection, graph execution limits, physical unknown-outcome handling,
   and the prohibition on physical retries and nested graphs.
3. Kept `run_limited_graph` immediately loaded when ordinary Skill schemas use
   deferred loading. This makes graph invocation a normal FunctionTool call
   without a preceding graph-schema lookup being mistaken for execution.
4. Strengthened Agent instructions to prefer and submit Limited Graph before
   directly invoking children for every predetermined workflow containing two
   or more known finite Skills. Routed Agents receive the graph-first reminder
   after their route-specific instructions. Direct calls remain for one Skill,
   open-ended replanning, and non-graphable host setup.
5. Updated the Limited Graph Skill metadata, contract, manifest policy, UI
   prompt, configuration templates, changelogs, and regression tests with the
   same behavior.

### Supersession validation boundary

Software tests verify tool exposure, broker eligibility, and instruction
contracts. They do not prove successful physical execution; hardware
qualification must still inspect the graph trace, exact child call identity,
authorization result, physical-action count, and terminal outcome.

The focused graph integration set passed 40 tests. The complete Test Agent set
passed 438 tests and 27 subtests, the standalone graph runner passed 13 tests,
and the configured repository package-root run passed 1,080 tests and 27
subtests. Skill metadata/JSON validation, documentation validation,
configuration baselines, and Python environment isolation also passed.

## 2026-08-16 — Limited Graph false success and compound-route exclusion

### False-success retained evidence

The newest retained runs prove that Limited Graph was invoked. Run
`dc2d8cd3-20d4-4efb-9f37-2a1fd84211ca` called it at
`2026-08-16T10:04:09.279018Z`; run
`c7f8d05e-1181-4b0a-9c86-40849b4ea889` called it at
`2026-08-16T10:06:21.751853Z`.

The first graph's motion child returned `INTEGRATED_RECOVERY_REQUIRED` with
`workflow_complete=false`. The second graph's motion child returned
`IK_PREVIEW_REJECTED` with `workflow_complete=false` and controller reason
`SHADOW_PLANNING_TIME_BUDGET_EXCEEDED`. Both graphs incorrectly entered their
`COMPLETED` terminals because a normal FunctionTool return unconditionally
selected `next_node`.

The prompt also requested slicing. The selected deterministic route exposed
the scene/corner tools but excluded the direction translator and slicing
Skill. Retained tool-search message 9318 requested
`slice_with_blade` and `translate_fabric_direction_to_world`; message 9319
returned `tools: []`. From prompt message 9308 onward, the session contains two
`run_limited_graph` calls, zero `slice_with_blade` calls, and zero
`translate_fabric_direction_to_world` calls. The slicing Skill therefore did
not reject this run; it was never invoked.

### False-success implementation checkpoints

1. Explicit incomplete child results now follow `failure_node` and emit
   `CHILD_RESULT_INCOMPLETE`; incomplete/retrying nodes are not recorded as the
   last completed node.
2. Added a compound mapping + corner motion + mixed-frame slicing route that
   retains all graph-eligible child tools needed by the complete request.
3. Strengthened the Agent and Limited Graph instructions against prefix-only
   graphs and against successful terminals before every requested stage.
4. Preserved no-retry and no-cycle rules for physical nodes while allowing
   separately requested physical actions to occupy distinct predetermined
   nodes.
5. Recorded the independently observed semantic-scene frame reversal in
   `docs/incidents/2026-08-16-semantic-scene-arm-base-reversal.md` without
   changing the scene, calibration, or slicing Skills.

### False-success validation boundary

The focused standalone runner passed 17 tests. The Agent discovery, route,
broker, and external-host set passed 44 tests. The complete Test Agent suite
passed 439 tests and 27 subtests. The configured repository package-root run
passed 1,085 tests and 27 subtests. The Skill Creator validator, 133-file
documentation check, 103-file JSON parser, configuration-baseline audit, and
Python environment-isolation audit passed. No hardware operation was submitted
during this correction.

## 2026-08-16 — Superseded workcell transform remained scene-usable

### Retained-run evidence

FoundationPose run `dcefa1fe-444e-40c2-8f77-7b6840e03d58` activated
calibration `20260816T103534Z-731739f9` as activation
`4c6633cd-71c5-4415-8d60-99374979a0ec`. Its camera-to-base rotation was
178.963 degrees from the superseded calibration
`20260816T103200Z-a36cc21a`, and only 6.676 degrees from the earlier working
calibration `20260816T083353Z-2f8d4a5e`. The newest FoundationPose result had
therefore returned to the earlier working orientation family.

The following scene run `8d617143-0646-4e10-a44d-92f8b39477f5` nevertheless
published toilet-paper source point
`[-0.3539274563832034, -0.16919827814410315, 0.19585638616188855]` in
`rebot_arm_base`. That point maps to camera point
`[0.04956230140526935, 0.06810421003686545, 0.7748186335511873]` under the
superseded transform, but to
`[0.509572115008919, -0.20093404149065813, 1.3551369774520527]` under the
replacement transform. The point was generated in the superseded arm-base
axis and later converted to world with the replacement activation.

### Activation-boundary defect

Manager published the replacement activation to Fabric and changed every
prior active record to `SUPERSEDED` only in its in-memory record map. It did
not publish revoked transform envelopes for those superseded records. Fabric
therefore retained the old reviewed camera, VIO, and arm-base graph alongside
the replacement graph, allowing a camera-to-arm-base query to traverse the
expired calibration.

### Implementation checkpoints

1. Manager now materializes the complete set of superseded activation records
   before committing the replacement.
2. It builds one Fabric observation batch containing three revoked transform
   envelopes plus an inactive activation envelope for every superseded record,
   followed by the replacement's three active transforms and active envelope.
3. All envelopes have unique sequence numbers, while the whole transition
   shares one observation time.
4. Manager commits the superseded and replacement records only after Fabric
   accepts the complete batch.
5. The regression verifies two prior activations produce revoked,
   non-motion-usable transforms and that the replacement remains the final
   active portion of the twelve-observation batch.

### Validation boundary

The focused supersession regression passed. The complete Manager suite passed
53 tests and the complete Fabric suite passed 26 tests. Repository validation
then passed the 133-file documentation check, 38-source configuration audit,
Python environment-isolation audit, 103-file JSON parse, both Rust suites, and
the optimized Manager release build. Python package tests were not rerun for
this Rust/documentation-only correction; the immediately preceding configured
repository run had passed 1,085 tests and 27 subtests. No physical motion was
submitted during diagnosis or correction.

### FoundationPose mask follow-up

The same retained FoundationPose run exposed a separate orientation risk. The
saved gripper masks for calibrations `20260816T103200Z-a36cc21a` and
`20260816T103534Z-731739f9` covered the arm shoulder/background instead of
reliable gripper support. Their VLM localization confidences were 0.58 and
0.56. The second calibration happened to return to the earlier working
orientation family, but both masks were below a defensible axis-decision
threshold.

The alignment Skill now requires the configured
`minimum_gripper_axis_confidence`, default 0.70, before segmented gripper depth
may resolve the 0/180-degree base-axis ambiguity. An untrusted mask is retained
as evidence and a warning, while the existing bounded RGB overlay review makes
the discrete orientation decision. Unit coverage verifies the threshold and
the complete fallback route; the focused mode file passed 12 tests and the
complete alignment Skill suite passed 99 tests after this addition.

## 2026-08-16 — Limited Graph Provider handover live checkpoint

### Implementation checkpoint

The hosted broker now follows only an exact, typed
`set_provider_residency` continuation returned by a child before that child has
authorized or submitted physical motion. The lifecycle request uses the
existing Agent FunctionTool and retains its schema validation, enablement,
approval callback, Manager client, readiness wait, session authorization, and
deadline. After readiness, the broker invokes the same child with unchanged
arguments and a fresh call ID. One logical child call permits at most two
distinct Provider handovers; a repeated handover, post-authorization handover,
invalid lifecycle result, or incomplete readiness stops on the child's failure
path. Graph topology validation now also rejects every cycle containing a
physical child.

### Retained live evidence

Agent run `0eb0141c-9ea5-42af-8647-8623079977d4` submitted two Limited Graphs
for one scene, corner-motion, slicing, repositioning, and repeat-slicing
request. Graph `78ffe22ab72e4a9caae6d005c7ab7806`, digest
`f3c71d739097154c799d3b3fa36211679b02f30162267c9bb6a822458f60678f`,
completed the corner derivation, first motion, direction translation, and first
slicing action. It then failed explicitly at `move-above-first-point` because
binding source `/outward_retract_end_position_world_m` did not exist in the
completed slicing result. The runner emitted the exact `GraphValidationError`,
did not retry the physical node, and retained `first-slice` as the last
completed node.

Graph `c88d4f6b5725467e901ddf1a55cb5445`, digest
`23d08ea0e9b3bc37375d616cb07cbac29a166c6c9d1fc47aa5cdaf36d7bb03cc`,
used the requested slice-start point plus 0.1 m world Z and completed both
`move-above-first-point` and `repeat-slice`. The movement child requested
Integrated Provider capability `robot_arm.motion.free_space.preview_commit.v1`.
The retained trace contains ordered `PROVIDER_HANDOVER_STARTED` and
`PROVIDER_HANDOVER_COMPLETED` events for lifecycle call
`c88d4f6b5725467e901ddf1a55cb5445:move-above-first-point:1:provider:1`;
the resumed child returned `MOTION_COMPLETED`, and the repeat slicing child
then returned `SLICING_SEQUENCE_SUBMITTED_AND_RELAX_REQUESTED` with
`workflow_complete=true`. Both graphs recorded two physical child calls and no
graph retry.

The run retained calibration revision
`20260816T124518Z-c4b4f504`, activation
`ffd36943-1df5-41d1-ba57-3d0462d9fc56`, and session epoch
`f4b99f8c-343a-4ff9-8e1a-9c7f2245887d`. Structured evidence contains no use
of a superseded arm-base transform. The slicing Skill was invoked twice and
was not blocked by collision detection.

### Stable-with-known-flaws boundary

This checkpoint is suitable for preservation because software validation is
clean, the live lifecycle continuation completed through the intended Manager
boundary, the slicing sequence completed twice, current spatial provenance was
retained, and the malformed graph failed explicitly without unsafe physical
retry. It is not a claim that every graph behavior is qualified.

Known limitations at this checkpoint are:

- child result fields are not discoverable as an output schema before graph
  submission, so a syntactically valid JSON pointer may fail only after its
  source child completes;
- lifecycle trace events are delivered to the runner after the broker returns,
  so the start and completion events currently share the broker's final
  elapsed time and do not measure lifecycle duration;
- the initial Provider preparation before the first graph remained legal
  direct host setup rather than graph-managed handover; and
- no retained live test selected an edge with `FAST_TEXT`, `FAST_VISION`, or a
  custom local routing profile.

Output-schema discovery and naming are intentionally deferred to the next
development checkpoint.

### Validation checkpoint

The final standalone runner passed 19 tests. The hosted broker and routing set
passed 16 tests. The complete Test Agent suite passed 446 tests and 27
subtests, and the configured repository Python run passed 1,096 tests and 27
subtests. Python compilation, Skill Creator validation, documentation
integrity, configuration baselines, JSON parsing, environment isolation, Rust
Manager/Fabric tests, and the optimized Manager release build passed in the
corresponding implementation checkpoints.

## 2026-08-16 — Mandatory installed-Skill output schemas

### Contract checkpoint

Agent Skill discovery schema version 2 makes a normalized JSON-object
`output_schema` mandatory for every installed Skill, including manual-only and
normally non-discoverable Skills. The schema is a platform discovery contract
for catalogs, direct calls, replay, and finite composition; it is not a
Limited Graph ownership rule. Declared fields do not change Skill
responsibility, Provider selection, Manager lifecycle authority,
authentication transport, signed-action handling, or physical authorization.

Only explicit JSON Schema `properties` and their array item schemas publish
stable composition paths. `additionalProperties=true` may preserve diagnostic
extensions in a returned object but does not publish arbitrary names as graph
bindings. Output schemas are embedded and self-contained in version 2 so
catalog inspection and static graph validation do not depend on an external
schema resolver.

### Installation migration checkpoint

1. Migrated all 22 descriptors installed at that checkpoint to discovery
   schema version 2 and added an object-root output schema to each one.
2. Preserved every Skill's existing package version. Discovery schema version
   2 is the compatibility signal for the new metadata; the migration does not
   claim that the 22 independently versioned Skill implementations changed.
3. Declared the slicing result using its actual nested result structure,
   including `/plan/path/slice_begin_point_world_m`,
   `/plan/path/slice_endpoint_world_m`, and
   `/plan/path/planned_retract_endpoint_world_m`. The previously invented
   `/outward_retract_end_position_world_m` name is not declared.
4. The then-current complete Test Agent run exposed one concrete migration
   error (this was the only mismatch exercised by those tests, not an
   exhaustive source-semantic audit):
   `camera_system_point_m` had initially been described as an array even though
   the spatial-registration adapters return an object with
   `camera_system_x`, `camera_system_y`, and `camera_system_z`. Both affected
   manifests were corrected, and the rerun passed.

### Host checkpoint

The catalog now rejects legacy discovery metadata, missing output schemas,
non-object schema roots, malformed JSON Schemas, and output schemas containing
external or dynamic references. Every hosted child descriptor carries a deep
copy of the declared output schema. Direct FunctionTool results are normalized
from JSON text when necessary and validated before being returned as a
successful Skill result. Agent-facing descriptions append the exact bindable
result pointers, including complete object or array properties rather than
only scalar leaves.

The two special in-process motion tools use the same result validation and
pointer publication as generic hosted Skills. These checks are downstream of
their existing preparation, authorization, and execution boundaries and do not
grant or synthesize authority.

### Output-schema validation checkpoint

The standalone Limited Graph runner passed 24 focused tests. Agent discovery,
external-host, and hosted-broker coverage passed 52 tests after adding the
mandatory-missing-schema regression. The complete Test Agent run passed 450
tests and 27 subtests, and the configured repository package-root run passed
1,105 tests and 27 subtests. Python compilation, the Skill Creator validator,
the 133-file documentation check, 103-file JSON parser, configuration-baseline
audit, and Python environment-isolation audit passed. No Provider, Manager
runtime, or robot process was started.

## 2026-08-16 — Source-backed output-schema semantic audit

### Why the first migration was not sufficient

The mandatory-schema checkpoint proved that all 22 then-installed descriptors had
valid discovery-v2 metadata and that the host and Limited Graph could enforce
it. It did not yet prove that every published property name matched every
actual adapter return. The initial schemas were intentionally permissive, and
several were drafted from Skill purpose rather than exhaustively traced from
the registered Agent adapter. That left graph authors vulnerable to plausible
but nonexistent names even though schema presence itself was stable.

This audit traced the registered adapter, its result-producing implementation,
and every explicit success, failure, recovery, or call-scoped continuation
shape. The audit does not change Skill duties, Provider responsibility,
authentication transport, Manager lifecycle authority, or physical-action
authorization.

### Per-Skill source map and disposition

| Agent tool | Agent-visible result source | Audit disposition |
| --- | --- | --- |
| `plan_no_contact_item_approach` | `NoContactItemApproachAdapter.run` and preview attachment | Added complete correction-plan geometry, evidence, preview, closest-safe, and recovery fields. |
| `derive_fabric_world_point` | `FabricWorldPointComposer.run` | Added schema identity, temporal decision, selected-scene disposition, source identity, and typed failure fields. |
| `establish_world_axis` | `InitializeSpaceCognitionSkill.ensure_tracking` | Removed invented workflow and continuation fields; declared the exact `tracking_ready` wrapper and nested tracking result. |
| `execute_reviewed_observation_motion` | `ReviewedObservationExecutionAdapter.run` | Removed the invented controller wrapper and physical flags; declared the exact decision-bound controller result. |
| `localize_known_cad_object` | No direct adapter; `FiniteFoundationPoseRuntime` is nested and manual-only | Replaced the invented direct result with an explicit empty direct contract while discovery remains disabled. |
| `identify_pointed_object` | `PointingIdentificationSkill.run` | Declared success, Provider-activation, camera-unavailable, retry, route, binding, and annotation-processing variants. |
| `reinitialize_space_cognition` | Application wrapper around `InitializeSpaceCognitionSkill.run(force_reset=True)` | Removed invented workflow and continuation fields; declared the reset result and application-added fields. |
| `inspect_arm_semantic_scene` | `SemanticSceneInspector.run` | Removed nonexistent session epoch; declared mapping, tracker recovery, scene identity, counts, AABBs, spheres, production, and evidence fields. |
| `perform_relative_effector_motion` | Call-scoped coordinator plus `IntegratedRelativeMotionAdapter.preview` and `execute_preview` | Replaced nonexistent position aliases and controller wrapper with preparation, recovery, preview, state-loss, commit, completion, and verification fields. |
| `run_limited_graph` | `LimitedGraphRunner.run` | Verified manifest properties and required fields against the canonical Limited Graph result schema. |
| `locate_effector_front` | `EffectorFrontSkillAdapter.run` plus effector landmark resolver | Replaced generic point aliases with `front_points`, `control_reference.target_point_m`, controller consistency, FK fallback, route, and temporal evidence. |
| `move_effector_to_world_point` | Call-scoped coordinator plus `preview_world_point` and `execute_preview` | Declared absolute-world identity and resolution together with bounded preparation and final controller result fields. |
| `locate_item` | `MetricItemLocatorAdapter.run` plus item locator | Replaced generic point, bearing, and Fabric aliases with `location`, `bearing`, depth/task-plane/volume evidence, visual support, and semantic-scene assertion. |
| `refine_arm_root_translation` | External host adapter, Skill runtime, and refinement math | Replaced invented sample/correction/manager names with the real translation, multi-sample, reselection, review, dependency, and state-update fields. |
| `register_tool_to_control_frame` | `ToolControlFrameSkillAdapter.run` plus registration candidate builder | Replaced invented candidate/control-frame fields with the exact review-only transform, landmarks, quality, bindings, provenance, and route fields. |
| `slice_with_blade` | `SlicingHostAdapter.invoke` and `SlicingPlan.as_dict` | Restricted the Agent result to the exact completed command-handling envelope and fully declared the nested path and measured-start deltas. |
| `register_rgbd_pixel_to_world` | `SpatialRegistrationSkillAdapter.run` plus `register_rgbd_point` | Replaced generic aliases with RGB/depth pixels, camera-system XYZ, `target_point_m`, selection, binding, transform, and temporal evidence. |
| `calibrate_stationary_workcell` | `StationaryCalibrationSkillAdapter.run` plus `AlignmentSkill.run` | Declared explicit-name rejection, unobservable-pose recovery, candidate result, activation continuation, and adapter provenance. |
| `translate_fabric_direction_to_world` | `FabricSpatialTranslator.translate_direction` | Removed nonexistent root transform path; declared exact framed direction and typed frame-authority failures. |
| `translate_fabric_pose_to_world` | `FabricSpatialTranslator.translate_pose` | Removed nonexistent root transform path; declared exact framed pose and typed frame-authority failures. |
| `verify_rgbd_image_alignment` | `RgbdAlignmentValidationSkill.run` | Replaced a different review vocabulary with actual numeric quality, VLM review/route, geometry, timing, binding, and artifacts. |
| `analyze_visual_scene` | `PointingIdentificationSkill.run` | Applied the same complete output union as pointed-object identification. |

### Regression boundary

`test_skill_output_contract_audit.py` began as a checked-in 22-Skill coverage
map and now requires an exact entry for every installed descriptor, including
the subsequently added twenty-third Skill.
It requires one audit entry per installed descriptor, verifies every referenced
runtime source and identifying token, compiles every output schema, checks
important declared and forbidden root names, confirms that the nested
FoundationPose primitive has no invented direct output, and keeps the Limited
Graph manifest aligned with its canonical result schema. Agent discovery tests
now use runtime-shaped result doubles instead of abbreviated payloads that no
real adapter returns.

Schemas keep `additionalProperties=true` only where real typed failure payloads
or subordinate Provider/controller diagnostics are intentionally extensible.
An open extension remains non-bindable by Limited Graph; graph bindings still
require an explicitly declared path. Exact fixed envelopes use closed roots so
new unreviewed names fail direct host validation.

### Semantic-audit validation checkpoint

The dedicated then-22-Skill source audit passed 2 tests. The complete Test Agent
suite then passed 452 tests and 27 subtests, and the configured repository
package-root suite passed 1,107 tests and 27 subtests. The Limited Graph wheel
built successfully without build isolation, and the Skill Creator validator
accepted the package. Python compilation, the 133-file documentation check,
103-file duplicate-key-rejecting JSON parse, configuration-baseline audit, and
Python environment-isolation audit passed. No Provider, Manager runtime, robot
process, or physical action was started.

## 2026-08-16 — Live stationary-gate contract correction

### Retained failure evidence

Two consecutive live Agent runs selected `run_limited_graph` for the requested
world-axis, FoundationPose, refinement, and motion workflow. Graph runs
`f3a106a8e56c43f3a1f10ea4bb96de9a` and
`9ebe8fb8d2ba4fa99ec3b87280213549` both stopped at the first
`establish_world_axis` child. The child returned valid runtime strings
`GLOBAL_MOTION_INHIBIT` and `EXISTING_TRACKING_EPOCH`, respectively, while the
new output schema incorrectly declared `/result/stationary_gate` as an object.
The runner recorded `CHILD_FAILED`, selected the declared failure edge, retained
no invalid node result, and submitted no physical action.

Historical run `9d1552c6-c73e-4cfb-a522-fbf672b983ef` used the same complete
operator prompt before output-schema enforcement. Its first graph
`00f1b1a5828f4dd1ae3238bc8731d0bf` likewise stopped at the FoundationPose
candidate boundary, after which the Agent performed the mandatory host review
and activation and submitted post-calibration graph
`2cfeb1d5bd924f25a466f5e321ab465d`. That second graph completed the requested
raise, five-sample refinement, forward motion, and down motion. The staged
first-graph topology in the failed live runs is therefore not a new prefix-only
planning regression; the incorrect world-axis contract prevented the existing
post-review continuation from being reached.

### Correction and regression boundary

The `establish_world_axis` output contract now declares the two exact string
values produced by `InitializeSpaceCognitionSkill.ensure_tracking`. The
initialization tests validate real results from both the existing-epoch and
motion-inhibit branches against the installed discovery schema. The then-22-Skill
source audit also pins both branch tokens so a future edit cannot silently
erase either contract branch while leaving only a syntactically valid schema.

This correction changes no Skill duty, Provider lifecycle ownership,
authentication transport, Manager authority, FoundationPose review boundary,
or physical authorization. Limited Graph's fail-closed behavior was correct;
the published child result contract was not.

The focused initialization and output-contract set passed 11 tests. The
complete Test Agent suite passed 453 tests and 27 subtests, and the configured
repository package-root suite passed 1,108 tests and 27 subtests. These were
stopped-software validations; no Provider, Manager runtime, robot process, or
physical action was started.

## 2026-08-17 — Live graph visuals and multi-evidence chat turns

The retained successful retest separated graph correctness from presentation.
Run `5eefde4d-20b7-4cc1-b327-463cd1abec87` completed its eight-node compound
SAM2/corner/two-slice graph with four physical actions, one visit per node, and
no retries. The SAM2 visual existed in the inspection child result, but the
journal published it only when `run_limited_graph` completed. The earlier
single-value chat projection then allowed a subsequent visual to replace it.

The Agent driver now binds a run-local hosted-child event sink while the SDK
run is active. Limited Graph's child observer forwards each validated and
redacted result to the hosted broker, which reuses the standard visual-evidence
sanitizer and emits only `visual.evidence.created`. A context-local binding
keeps concurrent Agent runs isolated. The final graph tool output still passes
through the ordinary event translator for replay and compatibility, while a
run-local evidence-ID set suppresses duplicate live/final delivery.

The chat API now projects an ordered, unique `visual_evidences` list for each
turn, bounded to the newest 32 objects. It also preserves the last object in
the original `visual_evidence` field for older clients. The shared browser
component keeps a map of viewer instances keyed by evidence identity and
renders one card per object on both regular and developer Agent pages. Server
hydration accepts both the new list and historical single-object sessions.

The presentation relay is deliberately downstream of Skill result validation
and credential redaction. It has no graph-routing, retry, Provider, Fabric,
Manager, authentication, session, motion-authorization, or controller role.
Observer exceptions cannot fail a graph, and unsafe or private visual fields
remain excluded by the established sanitizer.

Focused regressions cover immediate hosted-child publication, visual
sanitization, duplicate suppression, two-object session projection, legacy
compatibility, and the multi-card browser contract. A temporary stopped-
software browser harness additionally rendered two simultaneous visual cards
and confirmed their independent titles in the DOM. No live Provider, Manager,
robot process, or physical action was started by this validation.

The final stopped-software checkpoint passed 463 Test Agent tests and 27
subtests, 28 Limited Graph engine tests, and compilation of all modified Python
modules.

The complete repository validator then passed 1,124 Python tests and 27
subtests, 79 Rust tests, every configured Python wheel build, release Manager
compilation, configuration and environment-isolation checks, JSON and
documentation validation, and source-integrity refresh.

## 2026-08-17 — Graph movement rejection and aligner visual restoration

### Graph rejection separation

The latest retained runs contained two different failures that initially
appeared Fabric-related. Fabric point derivation itself succeeded with a fresh
scene in graph `42f784c933b64753be8a5abb7504ff73`. The following physical
movement completed but Limited Graph rejected its result because credential
redaction changed the required object-valued `authorization` field into a
string before schema validation. Graph `5beaa4e662fd405597621ec4107db458`
failed through the same post-motion path after Provider handover.

The combined corner-motion and slicing run
`98a689d8-5ebd-4cfc-908e-25abb70d77ee` failed earlier at preflight because the
mixed-frame slicing discovery route excluded `derive_fabric_world_point` and
`move_effector_to_world_point`. A dedicated compound route now preserves the
complete Skill union and requires a single complete graph. Fabric ownership,
freshness checks, coordinate translation, Provider lifecycle duty,
authentication, and physical authorization are unchanged.

### Visual-evidence separation

FoundationPose/VLM artifacts were generated successfully under the alignment
run directory, including RGB, depth, VLM overlay, and selected pose overlay.
The Agent adapter did not register them with the Test Agent's
`VisualEvidenceStore`, so no `visual.evidence.created` event reached either
Agent surface. The standalone aligner page had a second process-boundary issue:
it read only its own in-memory `MonitorArtifacts`, while Agent-triggered runs
used a different `AlignmentSkill` instance.

The adapter now registers up to four persisted channels and returns the standard
root `visual_evidence` object consumed by Agent event translation. The aligner
page reads the latest persisted run images when its local Skill is not running,
validates the alignment identifier, reconstructs only fixed child paths under
the configured run root, and never trusts the stored absolute overlay path.
While a page-local run is active, its in-memory artifacts retain priority.

The focused visual-evidence, event, schema-audit, and aligner-GUI checkpoint
passed 30 tests and 9 subtests. No Provider, Manager runtime, robot process, or
physical action was started by this validation.

The complete Test Agent suite then passed 455 tests and 27 subtests. The
configured repository package-root suite passed 1,114 tests and 27 subtests.
Python compilation, 134-file documentation integrity, 104-file
duplicate-key-rejecting JSON parsing, configuration baselines, and Python
environment isolation passed. Source integrity manifests were refreshed. No
Provider, Manager runtime, robot process, or physical action was started by
these validations. Limited Graph, stationary alignment, and Test Agent wheels
built successfully from the final source.

## 2026-08-17 — Nested graph visuals, typed point offsets, and Safe Home routing

### Retained evidence and failure ownership

Agent run `e6ca539b-ca48-416e-b948-eb06b48b694a` completed its
post-calibration VLM refinement and physical sequence. The FoundationPose and
refinement graph node results each contained standard visual evidence, but the
journal contained no visual event. The event translator decoded only JSON and
inspected only a tool result's root, while Limited Graph stores child outputs
under `node_results`. Compound scene routes also omitted the explicit semantic
scene inspector and used point derivation's intentionally nonvisual internal
inspection, so they generated no SAM2 evidence.

Run `0968b0a2-bb00-442d-a201-3a9cfcb23a89` denied a reposition defined as
the first slicing point plus world +Z 10 cm because graph bindings copy values
and no child Skill expressed that operation. Run
`6ee0c08e-a27c-4158-9c7f-daaa92c4f155` then completed graph
`63dca45efad24ec5afdbb02331c720cc` with the wrong authored binding:
`/plan/path/planned_retract_endpoint_world_m`. The engine did not compute or
select that point. It executed the submitted graph, whose source field meant
the slicer's retract endpoint rather than the requested point above
`/plan/path/slice_begin_point_world_m`.

Compound run `352f70b6-5c6a-49b1-913e-2ea9354386bf` hid the registered Safe
Home host operation on its routed surface, and standalone run
`ef0091d7-2746-4507-b2c0-c87705db3eb1` falsely denied the same operation on
the broad surface. The Basic adapter had a second weak boundary: disconnected
state raised free text instead of returning a typed Manager lifecycle
continuation.

### Corrections by boundary

1. Agent event translation accepts bounded JSON or Python literal dictionary
   representations, then projects sanitized root and child visual evidence.
   It deduplicates evidence identities and emits no raw graph result, paths,
   credentials, or private fields.
2. Compound scene-and-slicing routes place `inspect_arm_semantic_scene` first
   in the graph after direct policy and Provider setup. SAM2 therefore creates
   evidence, and the nested event projection makes it visible on both Agent
   pages.
3. The installed catalog now contains 23 output-schema-bearing descriptors.
   The new `offset_world_point` finite Skill is read-only and backed by
   `FabricSpatialTranslator`. It checks current world-frame and epoch
   authority, performs unit conversion and rotation under Fabric provenance,
   and exposes only coordinate evidence; it grants no motion or contact
   authority.
4. The Slicing output contract now publishes its actual nested workcell
   binding. A graph can bind `/plan/path/slice_begin_point_world_m` and
   `/plan/workcell_binding/world_frame` into the offset Skill, then bind its
   target point, frame, and epoch unchanged into absolute-world motion.
5. Standalone Safe Home receives a deterministic route with Limited Graph
   intentionally disabled because Safe Home is a host operation, not a graph
   child. Compound routes retain it only after graph completion. Disconnected
   Basic state returns the existing typed `set_provider_residency` shape for
   `robot_arm.rebot_dm`, HOT, and `robot_arm.safe_home`.

No change was made to graph-in-graph prohibition, graph limits, copy-only
bindings, Provider responsibilities, Manager authority, authentication,
session authorization, physical-action authorization, or controller safety
decisions.

### Regression checkpoint

The catalog-schema regression validates the exact
`slice_with_blade → offset_world_point → move_effector_to_world_point` graph
before child execution and rejects dependence on undeclared aliases. Focused
visual, spatial, discovery, host-broker, Safe Home, and audit coverage passed
102 tests. The complete Test Agent suite passed 461 tests and 27 subtests, and
the Limited Graph engine suite passed 26 tests. The configured repository
package-root suite passed 1,120 tests and 27 subtests, with 79 additional Rust
tests. Documentation integrity for 135 Markdown files, parsing for 105 JSON
files, configuration baselines, environment isolation, release Manager
compilation, and all configured wheel builds passed. These were
stopped-software checks; no Provider, Manager runtime, robot process, or
physical action was started.
