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
