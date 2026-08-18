# Limited Graph development record

This file records significant implementation checkpoints, validation evidence,
and known gaps while the Skill is under development. The normative behavior is
defined in `contracts/22_limited_skill_graph.md`; this record explains how the
current code reached that contract and where to investigate regressions.

## 2026-08-18: initial-value binding spelling compatibility

- Run `e3e0a083-e97f-4944-94e4-ddeed362c9c3` exposed a contradiction in the
  reference-host concise authoring surface. The model-facing instruction said
  `$initial#/pointer`, while the compiler treated the token after `$` as the
  initial-value name and therefore expected `$name#/pointer`.
- The Agent authored `$initial#/request` for initial value `request`. Static
  preflight correctly started no child, but the mismatch consumed the one
  pre-execution correction and the later invalid attempt terminated the run.
- The concise compiler now accepts both `$name#/pointer` and the equivalent
  namespace spelling `$initial#/name/pointer`. If an initial value is actually
  named `initial`, the original `$initial#/pointer` meaning is preserved.
  Compilation still produces the same canonical `source_kind`, `source_name`,
  and `source_pointer` fields before unchanged canonical validation.
- Forward runs `04ff2e46-5024-4ef0-bca0-734a39da19e5` and
  `e54aaecf-4b93-42b8-9f78-64559a87a73b` also verified the prior compact
  failure repair. Both complete cutting graphs published the exact first-
  Slicing rejection in `last_failure`, followed their declared failure edge,
  and performed no direct or later-stage continuation.
- The two cutting failures remain child-owned preview rejections: one
  singularity/IK-residual rejection and one shadow-planning time-budget
  rejection. This authoring repair does not change Slicing, Integrated, IK,
  physical retry, authorization, Provider, or Fabric behavior.
- Validation after the repair passed 485 Test Agent tests plus 27 subtests, 60
  standalone Limited Graph/Slicing tests, the Limited Graph package validator,
  141 documentation files, 1,150 repository-wide Python tests plus 27
  subtests, all Python wheel builds, and 80 Rust tests.

## 2026-08-18: compact terminal-failure visibility and graph ownership

- Autonomous run `2869b95b-a158-42fa-aa39-6e0c7056d8c0` proved the typed
  pre-submission path worked: the first Slicing node emitted
  `CHILD_PHYSICAL_ACTION_NOT_SUBMITTED`, selected `failed`, retained one
  completed physical action, and did not execute either later graph stage.
- The Agent-facing two-tier projection omitted `trace`, while the retained
  top-level message remained only `Limited Graph failed`. The Agent therefore
  lacked the failed node, child tool, rejection reason, and submission state.
  It guessed that Contact residency was missing and invoked the failed Slicing
  child directly outside the terminated graph. A repeated prompt in the same
  session produced a second direct Slicing attempt.
- Added mandatory compact `last_failure` data with `kind`, `node_id`,
  `tool_name`, `reason`, and `physical_action_submitted`. Full trace remains in
  the detail tier; the compact field removes the information gap without
  restoring trace bloat.
- Agent guidance now treats every non-success graph result as termination of
  that submitted workflow. Failed and remaining graph children must not be
  continued directly. Material replanning must use a new complete bounded
  graph, and a repeated user message is fresh unless it explicitly requests
  resumption.
- This change does not permit physical retry, change child authorization, move
  Provider responsibility, or modify Slicing, IK, collision, Fabric, or motion
  behavior.
- Focused Limited Graph/Test Agent regressions passed 90 tests. The complete
  Test Agent suite passed 481 tests and 27 subtests; the complete repository
  suite passed 1,146 tests and 27 subtests, 80 Rust tests, all Python wheel
  builds, 140 documentation files, 106 JSON files, source-integrity checks,
  and Skill Creator validation.

## 2026-08-18: live child pre-submission classification

- Live repeated slicing graphs compiled and ran under the explicit concise
  authoring format. Their first four nodes completed with the same first-slice
  arguments as the earlier successful canonical graph.
- Both runs received a Slicing-owned `IK_PREVIEW_REJECTED` exception before an
  Integrated preview ID or physical submission. The runner conservatively
  returned `UNKNOWN_OUTCOME` because the exception carried no typed submission
  evidence, so the declared failure edge was not selected.
- Added a trusted child-owner `physical_action_submitted=False` exception
  signal. The Test Agent broker converts only that signal into
  `ChildPhysicalActionNotSubmitted`; the runner records
  `CHILD_PHYSICAL_ACTION_NOT_SUBMITTED`, removes the unsubmitted invocation
  from the physical-action count, and follows the declared failure edge once.
- Unclassified exceptions, timeouts, cancellations, schema-invalid physical
  results, and possibly submitted operations retain `UNKNOWN_OUTCOME` and are
  never retried.
- Focused validation passed 31 runner tests, 21 host-broker tests, and 14
  Slicing host-adapter tests. The complete Test Agent suite passed 481 tests
  and 27 subtests; the complete Slicing suite passed 29 tests.

## 2026-08-17: reference-host concise authoring projection

- Kept the canonical manifest, runner, graph digest, validation and execution
  contract unchanged. The reference Test Agent now exposes a smaller strict
  authoring projection and deterministically compiles it to canonical version 1
  before the installed schema and all static preflight checks run.
- Ordered Skill steps imply ordinary success and failure edges. Bindings retain
  exact JSON pointers; edge overrides, read-only retries, switches, model
  routes, custom terminals and all six limits remain explicit when used.
- The exact retained eight-Skill two-cut graph round-tripped to the original
  canonical structure while shrinking from 6,451 to 3,749 serialized
  characters (41.9%). The model-facing schema shrank from 5,421 to 4,572
  compact characters (15.7%).
- Focused compiler, strict schema, concise end-to-end, canonical compatibility,
  Provider-handover, discovery and route validation passed 82 tests without
  starting runtime or physical processes.
- The complete Test Agent suite subsequently passed 477 tests and 27 subtests;
  Skill Creator validation passed, and repository documentation, configuration,
  environment-isolation, JSON and source-integrity checks completed.

## 2026-08-17: live concise JSON-field correction

- Two live runs failed before their first node because the shortened
  `initial[].value` field received raw operator text rather than encoded JSON.
  No child or physical action started.
- Renamed JSON-bearing projection fields to `value_json`, `args_json`, and
  `expected_json`. Added one model-visible `AUTHORING_INVALID` correction only
  for compilation or static preflight before all execution; a second invalid
  submission terminates.
- Added exact raw-text, corrected-resubmission, zero-child and bounded-second-
  rejection regressions. The reliable projection still reduces the retained
  two-cut graph by 41.3% and its model-facing schema by 10.1%.
- Focused validation passed 100 tests. The complete Test Agent suite passed 479
  tests and 27 subtests without starting runtime or physical processes.

## 2026-08-15: contract and standalone package

- Created the standalone `skills/limited-graph` Skill package with concise
  Agent instructions, manifest discovery metadata, a strict graph tool schema,
  result schema, authoring reference, package setup, and host adapter boundary.
- Fixed version 1 to sequential execution with immutable topology, bounded
  loops, no nested graphs, host-profile-only model routing, read-only retry,
  per-child authorization, and credential exclusion.
- Added `contracts/22_limited_skill_graph.md` before live-host integration so
  implementation deviations can be reviewed against a stable boundary.

## 2026-08-15: stopped-software runner

- Implemented canonical graph digests, reachability and terminal-path checks,
  JSON-pointer bindings, ordered switches, constrained model edges, result- or
  exception-driven read-only retry, physical unknown-outcome behavior, and all
  declared execution budgets.
- Added credential-like input rejection and result/error redaction before a
  value can enter graph state, a model route, trace, or final result.
- Added explicit events for node start/completion, child attempts, retry,
  selected edges, model fallback, authorization stops, exhausted limits,
  uncertain physical outcomes, and terminal completion.
- Initial validation exposed that the bundled workspace Python and the existing
  Test Agent environment do not install an asynchronous pytest plug-in. Tests
  were changed to use `asyncio.run`, avoiding a new hidden dependency.
- Validation command: `test_agent/.venv/Scripts/python.exe -m pytest -q
  skills/limited-graph/python/tests` with the Skill `python` directory on
  `PYTHONPATH`. Result: 10 passed.

## 2026-08-15: Agent host broker

- Added a late-bound broker handle so the external graph adapter can load before
  the Agent's complete FunctionTool surface exists, then bind once at startup.
- Child calls reuse the final direct-call FunctionTool callback. This preserves
  schema validation, call-scoped prepared-action code, timeout policy, and
  approval callbacks rather than invoking raw Provider adapters.
- The child catalog intersects the exact active Agent route. Limited Graph and
  manual-only Skills are always excluded. A separate boolean gate initially
  excluded physical children during the stopped-software rollout.
- Root session identity is copied into a unique child call context, while every
  exact child approval policy is evaluated again before invocation. Static
  authorization requirements stop version 1 with `AUTHORIZATION_REQUIRED`;
  automatic graph resume is intentionally not implemented.
- Model nodes resolve only pre-registered host profiles with a fixed modality.
  A graph supplies evidence and predeclared routes, but cannot supply a backend,
  model identifier, endpoint, or credential. With no profile, the runner uses
  the declared deterministic fallback.
- Model profiles receive only graph lineage/deadline metadata, not the root
  session authorization object. Dynamic FunctionTool enablement is checked
  immediately before each child. A host rejection before invocation follows
  the failure edge and is distinguished from an uncertain physical outcome.
- Added operational `FAST_TEXT` and `FAST_VISION` host profiles. `FAST_TEXT`
  uses an OpenAI Agents SDK Agent with no tools, one turn, low reasoning, and a
  strict Pydantic output type. `FAST_VISION` resolves at most four channels from
  the existing bounded visual-evidence store and parses a two-field route
  decision. Both remain subject to the graph deadline, confidence threshold,
  known-edge check, and deterministic fallback. The Agents SDK structured
  output choice follows current official OpenAI documentation.
- The callback registry also accepts a local model implementation. The graph
  never supplies that callback's endpoint, executable, model name, or secret.
- The structured-output implementation was checked against the official
  [OpenAI structured outputs guide](https://developers.openai.com/api/docs/guides/structured-outputs).
- Focused validation command: `test_agent/.venv/Scripts/python.exe -m pytest -q
  skills/limited-graph/python/tests
  test_agent/python/tests/test_limited_graph_host_broker.py
  test_agent/python/tests/test_external_skill_host.py`, with both Python package
  roots on `PYTHONPATH`. Result before the end-to-end case: 17 passed.
- Added repository adapter loading and a complete graph FunctionTool to child
  FunctionTool test. The focused total is now 19 passed.
- Broader Agent discovery/developer/UI validation initially produced one
  expected-list failure because the new discoverable tool was absent from the
  catalog baseline; the implementation behavior itself passed. The baseline
  was updated to include `run_limited_graph`. Python environment isolation and
  persistent configuration baseline audits both passed.
- The Skill validator passed in the Test Agent environment. The bundled
  workspace runtime could not run that validator because it lacks PyYAML.
- An editable install attempt in the existing Test Agent environment could not
  fetch its isolated build requirements because sandbox networking is disabled;
  focused validation therefore used explicit `PYTHONPATH`. Normal setup and CI
  install the declared build requirements before installing the package.
- Before operational model profiles were added, the complete repository Python
  surface passed 1,072 tests and 27 subtests in 54.17 seconds. After adding the
  text/vision profiles, the focused graph, routing, discovery, external-host,
  and UI set passed 87 tests before the final complete run.
- Final complete Python validation after operational model routing passed 1,075
  tests and 27 subtests in 53.51 seconds.
- A clean `midbrain_limited_graph_skill-0.1.0-py3-none-any.whl` was built with
  the repository validation environment, and the Skill Creator validator
  reported `Skill is valid!`. Temporary package build directories were removed;
  the ignored validation wheel remains available for local inspection.
- Installed that local wheel into the existing Test Agent environment without
  network access. A clean-process app smoke import confirmed that the Agent
  exposes `run_limited_graph` and registers exactly `FAST_TEXT` and
  `FAST_VISION` after normal configuration loading.

## 2026-08-15: post-reboot test-drive setup

- The OS update left `py.exe` installed but unable to find the still-working
  Python 3.11 installation. Test Agent setup now probes `py -3.11`, resolves
  its actual interpreter when usable, and otherwise falls back to a verified
  `python.exe` version 3.11 or newer. The normal no-argument setup completed
  through this fallback after the change.
- A fresh Skill-private environment was created with
  `skills/limited-graph/scripts/setup.ps1`. The first sandboxed dependency
  fetch was denied as expected; the same declared setup completed after
  dependency-network approval. No runtime or robot process was started.
- Rebuilt `midbrain_limited_graph_skill-0.1.0-py3-none-any.whl`. Its SHA-256 is
  `335a2e92fe965dc9c071d1fe5151ceb78fe3f217e653cf222ccf04a0c008712d`
  for the final isolated validation build.
- The Skill-private runner suite passed 13 tests. The integrated runner,
  broker, routing, external-host, and discovery set passed 54 tests. The Skill
  Creator validator reported `Skill is valid!`.
- A clean application import confirmed that `run_limited_graph` is offered and
  that the registered routing profiles are exactly `FAST_TEXT` and
  `FAST_VISION`. Physical graph children were still disabled at this checkpoint.
- Complete repository Python validation passed 1,075 tests and 27 subtests in
  53.62 seconds against the current validation environment, including the
  current compatible Agents SDK. Documentation, configuration baselines,
  environment isolation, 103 JSON files, all Python wheels, and source
  integrity manifest refresh also passed. Rust was not rerun because this
  test-drive setup changed only Python, PowerShell, Markdown, and manifests.

## 2026-08-16: physical-child gate removal and graph-first guidance

- Retained Agent logs showed a hosted tool search loading the
  `run_limited_graph` schema without a subsequent graph FunctionTool call.
  Because deferred discovery was visually easy to confuse with execution,
  `run_limited_graph` now remains immediately loaded while ordinary eligible
  child Skills may remain deferred.
- Removed the `LIMITED_GRAPH_ALLOW_PHYSICAL_CHILDREN` environment setting and
  the matching Settings, Agent-driver, and broker parameters. Physical child
  descriptors now enter the broker whenever they are eligible on the active
  routed Agent surface.
- Preserved the independent safety boundaries: nested graphs and manual-only
  children remain excluded, exact child authorization is re-evaluated,
  physical-action counts remain bounded, physical retries remain prohibited,
  and uncertain physical outcomes stop the graph.
- Strengthened the Skill metadata, Agent guidance, and UI default prompt to
  strongly prefer submitting a complete Limited Graph before calling child
  Skills directly whenever two or more known finite Skills form one
  predetermined workflow. A route-specific reminder is appended after the
  deterministic route instructions so a direct-call recipe cannot bury the
  graph-first policy. Necessary non-Skill host setup may remain direct.
- Added regression coverage for physical-child inclusion without a boolean
  gate, immediate graph schema availability under deferred child loading, and
  graph-first Agent guidance.
- Validation passed: 40 focused Agent discovery/broker/external-host tests,
  438 complete Test Agent tests plus 27 subtests, 13 standalone graph-runner
  tests, and 1,080 repository Python tests plus 27 subtests. The Skill Creator
  validator, manifest JSON parser, 132-file documentation check, configuration
  baseline audit, and Python environment-isolation audit also passed.

## 2026-08-16: retained-run false success and prefix-only graph correction

### Retained evidence

- Run `dc2d8cd3-20d4-4efb-9f37-2a1fd84211ca` invoked graph run
  `06f0b9ab50ba497ba92aa9826a562c85`. Its `move` child returned
  `INTEGRATED_RECOVERY_REQUIRED` with `workflow_complete=false`, but the runner
  followed `next_node` and returned `COMPLETED` after 47 ms.
- Run `c7f8d05e-1181-4b0a-9c86-40849b4ea889` invoked graph run
  `a963349d423940db87b4a7e7d1405dea`. Its `move` child returned
  `IK_PREVIEW_REJECTED`, `workflow_complete=false`, and
  `SHADOW_PLANNING_TIME_BUDGET_EXCEEDED`, but the runner again returned
  `COMPLETED` after 3,094 ms.
- Graph run `c0d27e5690314560b431a09e5330b6aa` showed the same defect for a
  non-motion child. FoundationPose returned
  `CANDIDATE_REVIEW_REQUIRED`, `workflow_complete=false`, and an exact
  `review_and_activate_stationary_calibration` continuation; the graph
  nevertheless entered its success terminal.
- The compound mapping, corner-motion, and cutting prompt selected a routed
  surface that exposed only the scene/corner prefix. Retained message 9318
  searched for `slice_with_blade` and
  `translate_fabric_direction_to_world`; retained message 9319 returned an
  empty tool list. The two affected runs invoked `run_limited_graph`, but the
  session contains zero `slice_with_blade` calls and zero
  `translate_fabric_direction_to_world` calls after the prompt.

### Incomplete-result implementation

1. The runner now interprets explicit `workflow_complete=false` as an
   incomplete child result and follows that node's failure edge. It also
   follows the failure edge for an explicit
   `physical_motion_completed=false` result from a physical child.
2. The runner emits `CHILD_RESULT_INCOMPLETE` with the child status, reason,
   attempt, and declared failure node. It no longer records an incomplete or
   retrying node as `last_completed_node`.
3. Deterministic routing now has a compound
   `SCENE_CORNER_MOTION_AND_MIXED_FRAME_SLICING` surface. It exposes scene
   setup plus derivation, absolute world-point motion, arm-base direction
   translation, slicing, ToolSearch, and Limited Graph so one submitted graph
   can contain the complete requested finite workflow.
4. Agent, Skill, manifest, UI, reference, and contract text now forbid a
   prefix-only graph. Every known requested graph-eligible stage must appear
   before a successful terminal. Separately requested physical operations use
   distinct predetermined nodes; physical retries and cycles remain forbidden.
5. Added regressions for FoundationPose-style review-required results,
   Integrated recovery/rejection results, successful physical completion, the
   complete compound route, and the strengthened graph-authoring guidance.

### Focused validation

The standalone runner passed 17 tests. The Agent discovery, route, broker, and
external-host set passed 44 tests. The complete Test Agent suite passed 439
tests and 27 subtests. The configured repository package-root run passed 1,085
tests and 27 subtests. The Skill Creator validator, 133-file documentation
check, 103-file JSON parser, configuration-baseline audit, and Python
environment-isolation audit passed. No Provider or robot action was started.

## 2026-08-16: child-declared Provider handover

### Boundary decision

- The existing contracts already permit a finite Skill to activate and switch
  between Providers while Manager retains lifecycle, scheduling, dependency,
  and physical-authority ownership. No root README or contract change was
  required for this checkpoint.
- Limited Graph does not select a Provider or infer a lifecycle operation. The
  hosted broker follows only an exact child result with
  `workflow_complete=false` and a typed `set_provider_residency` continuation.
  Provider lifecycle execution still uses the existing Agent FunctionTool and
  therefore its existing schema, enablement, approval callback, Manager client,
  readiness wait, and session authorization path.

### Provider-handover implementation

1. The hosted broker retains the existing `set_provider_residency` FunctionTool
   separately from the eligible child-Skill catalog. It is not graph-eligible
   and cannot be selected as a graph node.
2. For a physical child, automatic handover is accepted only before physical
   authorization or submission. The child must explicitly report
   `physical_motion_authorized=false`, and no physical request, submission, or
   completion field may be true.
3. Only the exact `hot` request emitted by the child is forwarded. Provider ID
   and required capability are copied unchanged. The graph cannot supply or
   override Provider arguments, credentials, endpoints, or authority.
4. After Manager reports the lifecycle request complete and Provider readiness
   is `READY`, the broker invokes the same child with unchanged arguments and a
   fresh call identity. Child enablement and authorization are evaluated again.
5. One logical child invocation permits at most two distinct Provider
   handovers. An identical repeated continuation stops after its first completed
   handover. Incomplete readiness, invalid lifecycle evidence, unavailable
   lifecycle tooling, and lifecycle timeout return the original incomplete
   child result to its failure edge rather than treating preparation as an
   uncertain physical outcome.
6. Graph traces now include `PROVIDER_HANDOVER_STARTED`,
   `PROVIDER_HANDOVER_COMPLETED`, and `PROVIDER_HANDOVER_FAILED` with bounded
   Provider, capability, call-identity, and reason evidence. Lifecycle
   preparation does not increment graph retry or physical-action counts.
7. Static validation now enforces the existing contract rule that no cycle may
   contain a physical node. This closes a runtime-only gap that could previously
   revisit a physical node until a general graph limit stopped the run.

### Validation evidence

- The standalone runner suite passed 19 tests, including Provider trace/count
  behavior and adversarial physical-cycle rejection.
- The hosted broker and routing suite passed 16 tests, including exact handover,
  unchanged continuation arguments, two ordered Provider switches, lifecycle
  authorization, incomplete readiness, repeated continuation,
  post-authorization rejection, and a full graph FunctionTool end-to-end
  handover.
- The complete Test Agent suite passed 446 tests and 27 subtests in 21.24
  seconds. No Provider, Manager runtime, or robot process was started.
- The configured repository package-root run passed 1,096 tests and 27
  subtests in 57.67 seconds. Python compilation, the Skill Creator validator,
  and the 133-file documentation integrity check also passed. Repository source
  integrity manifests were refreshed after the final code and documentation
  changes.

### Live qualification evidence

- Agent run `0eb0141c-9ea5-42af-8647-8623079977d4` exercised two physical
  graphs. The first, `78ffe22ab72e4a9caae6d005c7ab7806`, completed four
  child stages including one slicing action, then failed explicitly because
  `/outward_retract_end_position_world_m` was absent from the slicing result.
  The runner did not retry the physical child.
- The correction graph, `c88d4f6b5725467e901ddf1a55cb5445`, exercised the
  hosted Contact-to-Integrated lifecycle continuation. Its trace contains
  ordered `PROVIDER_HANDOVER_STARTED` and `PROVIDER_HANDOVER_COMPLETED` events,
  the same motion child resumed with a fresh call identity, the motion returned
  `MOTION_COMPLETED`, and the repeat slicing stage completed. Provider
  readiness and physical execution remained owned by the existing lifecycle
  and child FunctionTools.
- Both graph runs recorded two physical child calls and no graph retry. The
  Agent run retained current calibration revision
  `20260816T124518Z-c4b4f504`, activation
  `ffd36943-1df5-41d1-ba57-3d0462d9fc56`, and session epoch
  `f4b99f8c-343a-4ff9-8e1a-9c7f2245887d`.
- The live test did not exercise a model-selected edge. Handover trace entries
  are currently buffered until the broker returns, so their ordering and call
  identities are useful but their identical elapsed times do not measure the
  lifecycle duration.
- Child output fields are not yet discoverable before submission. The bad
  slicing pointer therefore passed graph schema validation and failed only
  after the source child had completed. Output-schema discovery is deferred to
  a separate development checkpoint.

## Current promotion state

- Stopped unit composition: implemented and passing focused tests.
- Read-only host composition: implemented and repository-tested.
- Stateful no-motion composition: implemented and qualified for the exact
  child-declared Provider `HOT` continuation exercised by the live run.
- Simulated physical composition: implemented and covered by routed-host and
  end-to-end broker tests.
- Developer physical qualification: passed for the retained compound
  Contact-to-Integrated handover and two slicing actions. This does not qualify
  model-selected branching, arbitrary output bindings, or every Provider
  sequence. Physical graph calls continue to use the same exact child
  authorization and prepared-action boundaries as direct calls.

When investigating a failure, record the graph digest, graph run ID, root and
child call IDs, terminal status, last completed node, exhausted limit if any,
and the ordered trace. Never add credentials or signed action tokens to this
record.

## 2026-08-16: declared child-output preflight

### Defect carried from the live checkpoint

The retained live graph used
`/outward_retract_end_position_world_m` as a slicing-result source. Because
child outputs had no discovery contract, graph submission accepted the name,
ran the first physical slicing child, and discovered the missing field only
when the later binding tried to read it. The runner stopped without repeating
the physical child, but the error was avoidably late and consumed an earlier
physical action.

### Implementation checkpoint

1. Limited Graph child descriptors now carry the same mandatory discovery-v2
   output schema used by direct Agent FunctionTools.
2. Static validation resolves every `NODE_RESULT` binding, retry condition,
   switch condition, and model-route input only through explicitly declared
   output properties and array items. It also resolves every binding target
   through the destination Skill's input schema.
3. Initial-value source pointers are checked against the submitted initial
   JSON value. Undeclared child paths and destination arguments fail graph
   validation before the first node is invoked.
4. Runtime validates each normalized child result against its declared schema
   before credential redaction, then redacts before retaining or routing it. A physical
   result mismatch is an uncertain physical outcome and returns
   `UNKNOWN_OUTCOME`; a read-only mismatch follows the existing bounded retry
   and failure behavior. Validation errors expose only the failing JSON pointer,
   validator, and expected schema value; they never include the invalid result
   instance.
5. Open schema extensions are valid diagnostic data but are not statically
   bindable. Optional declared fields can still be absent in a particular
   result variant, so graph authors must branch on completion/status before
   consuming success-only values.
6. Graph authoring guidance now tells the Agent to copy exact declared paths.
   It documents the real slicing paths and explicitly rejects the invented
   historical name.

### Output-schema preflight validation checkpoint

The standalone runner passed 24 tests, including valid nested result binding,
pre-execution rejection of an invented source, pre-execution rejection of an
invented target argument, and `UNKNOWN_OUTCOME` for a physical output-schema
mismatch. It also rejects undeclared retry, deterministic-switch, and
model-route input paths before invocation. The Agent
discovery/external-host/hosted-broker set passed 52 tests,
the complete Test Agent run passed 450 tests and 27 subtests, and the configured
repository package-root run passed 1,105 tests and 27 subtests. Python
compilation, the Skill Creator validator, documentation integrity, JSON
parsing, configuration baselines, and Python environment isolation also
passed. No Provider, Manager runtime, or robot process was started.

## 2026-08-16: semantic audit of every child result contract

The first discovery-v2 migration made output schemas mandatory and enabled
preflight, but schema presence did not prove semantic correctness. A complete
source audit subsequently traced all 22 descriptors installed at that checkpoint to the actual
registered adapter and result-producing implementation. Several plausible
names described no runtime output, including generic item and effector point
aliases, reviewed-motion `controller_result`, RGB-D alignment verdict aliases,
tool-registration candidate aliases, and root-level coordinate
`transform_path` fields.

The corrected contracts expose the actual composition paths. Important
examples are `/location/target_point_m` for a metric item,
`/control_reference/target_point_m` for the effector front,
`/target_point_m` for direct RGB-D registration,
`/framed_direction_world/transform_path` for translated-direction provenance,
and the existing `/plan/path/*` Slicing points. The nested FoundationPose
primitive remains undiscoverable and has an explicitly empty direct result
contract because no Agent adapter exists.

The audit regression began with those 22 Skills and binds every installed
descriptor, including later additions, to its source files
and representative source tokens, checks required and forbidden published
root fields, compiles each schema, and compares Limited Graph's own manifest
with its canonical result schema. This strengthens graph preflight without
changing child duties, Provider handover, credentials, authentication,
Manager authority, or physical authorization.

The dedicated audit passed 2 tests. The complete Test Agent suite passed 452
tests and 27 subtests, and the configured repository package-root suite passed
1,107 tests and 27 subtests. The Limited Graph wheel built successfully without
build isolation, the Skill Creator validator accepted the package, and Python
compilation, documentation integrity, duplicate-key-rejecting JSON parsing,
configuration baselines, and environment isolation passed. No Provider,
Manager runtime, robot process, or physical action was started.

## 2026-08-16: live child-contract failure checkpoint

Two live graphs failed before FoundationPose because the newly audited
`establish_world_axis` schema described `/result/stationary_gate` as an object,
but its registered `ensure_tracking` adapter produced the valid strings
`GLOBAL_MOTION_INHIBIT` and `EXISTING_TRACKING_EPOCH`. Graph run IDs
`f3a106a8e56c43f3a1f10ea4bb96de9a` and
`9ebe8fb8d2ba4fa99ec3b87280213549` each made one child attempt, emitted
`CHILD_FAILED`, selected the declared failure edge, and retained no invalid
result. Active runtime was 7,891 ms when stationary initialization was needed
and 93 ms when an existing tracking epoch was available.

Historical run `9d1552c6-c73e-4cfb-a522-fbf672b983ef` confirms that the short
initial graph is the expected staging boundary for this prompt. Its first graph
`00f1b1a5828f4dd1ae3238bc8731d0bf` produced the FoundationPose candidate; the
Agent then completed mandatory host review and activation before submitting
post-calibration graph `2cfeb1d5bd924f25a466f5e321ab465d`, which completed
the raise, five-sample refinement, forward motion, and down motion. The newest
runs never reached that continuation because the first child result was
rejected.

The graph engine behaved according to contract: it did not expose the invalid
result to a binding or later node, did not retry a stateful no-motion child
whose graph node allowed only one attempt, and did not submit physical motion.
The child manifest now declares the two exact string values, and runtime-shaped
tests validate both branches against discovery. This incident narrows the
remaining schema-audit risk from missing root names to incorrect nested branch
types and demonstrates why actual result validation must accompany static
source-token coverage.

The focused initialization and output-contract set passed 11 tests. The
complete Test Agent suite passed 453 tests and 27 subtests, and the configured
repository package-root suite passed 1,108 tests and 27 subtests. These were
stopped-software validations; no Provider, Manager runtime, robot process, or
physical action was started.

## 2026-08-17: post-motion authorization validation and combined-route correction

### Retained live evidence

Graph `5beaa4e662fd405597621ec4107db458` completed the Integrated Provider
handover and submitted one requested raise motion. The child returned a valid
object-valued `authorization`, but the runner replaced that entire object with
`[REDACTED]` before validating the declared output schema. Validation then
reported `/authorization` as a string where an object was required and returned
`UNKNOWN_OUTCOME`. Corner-move graph `42f784c933b64753be8a5abb7504ff73`
repeated the same post-motion failure after a fresh Fabric target derivation.

Run `98a689d8-5ebd-4cfc-908e-25abb70d77ee` exposed an independent preflight
failure. Its request combined an existing-scene work-object corner move with
mixed-frame slicing, but deterministic discovery selected the narrower slicing
route. The authored complete graph therefore contained
`derive_fabric_world_point`, which the host correctly rejected as ineligible.
This was a route-union defect, not a Fabric freshness, transform, or scene-data
failure.

### Corrections

1. The runner validates the normalized in-memory child result before redacting
   credential-like values. Only the redacted result is retained, bound, routed,
   traced, or returned.
2. Schema-error serialization reports structural location and expectation but
   omits the invalid instance, so validating before redaction cannot copy an
   authorization value into graph diagnostics.
3. A regression covers a physical result containing an object-valued signed
   authorization: the graph completes after one child call, retains only
   `[REDACTED]`, and exposes no token.
4. A second regression proves that an invalid authorization-shaped result
   returns `UNKNOWN_OUTCOME` without placing its value in the error.
5. Agent discovery now has a dedicated existing-scene work-object-motion plus
   mixed-frame-slicing route. It exposes Fabric point derivation, absolute-world
   motion, direction translation, slicing, lifecycle tools, and Limited Graph
   together and requires one complete graph.

The first focused runner and discovery checkpoint passed 60 tests. No Provider,
Manager runtime, robot process, or physical action was started by this
validation.

After the alignment visual-evidence integration, the complete Test Agent suite
passed 455 tests and 27 subtests and the configured repository package-root
suite passed 1,114 tests and 27 subtests. Python compilation, 134-file
documentation integrity, 104-file duplicate-key-rejecting JSON parsing,
configuration baselines, and Python environment isolation passed. Source
integrity manifests were refreshed. Limited Graph, stationary alignment, and
Test Agent wheels built successfully from the final source. No Provider, Manager
runtime, robot process, or physical action was started by these validations.

## 2026-08-17: child visuals and the missing point-offset operation

Retained run `e6ca539b-ca48-416e-b948-eb06b48b694a` proved that graph child
results held valid FoundationPose and VLM evidence while the UI event layer
discarded it. This was outside the graph runner: the runner correctly retained
the child objects under `node_results`, but the SDK event translator accepted
only JSON text and inspected only root visual evidence. The translator now
decodes a bounded Python literal representation when needed and sanitizes each
root or child evidence object into a separate UI event.

Runs `0968b0a2-bb00-442d-a201-3a9cfcb23a89` and
`6ee0c08e-a27c-4158-9c7f-daaa92c4f155` exposed a separate catalog gap. The
first correctly observed that copy-only bindings cannot calculate an earlier
point plus 10 cm. The second graph used a declared but semantically wrong
Slicing retract endpoint and completed a wrong reposition. Limited Graph did
not evaluate an expression or choose that path; the authored graph supplied
the wrong exact source pointer.

The correction preserves the copy-only contract. A new read-only
`offset_world_point` child accepts an upstream world point, its published frame
identity, and one typed displacement. Fabric owns unit conversion, current
frame validation, and arm-base or controlled-effector rotation. Slicing now
publishes its actual nested workcell world-frame field. Static validation
accepts the exact first-slice-point to offset to world-motion pointer chain,
while the route instruction explicitly distinguishes the first point from the
planned retract endpoint.

Compound scene routes now include `inspect_arm_semantic_scene` as the first
graph child after host setup so SAM2 evidence is produced before point
derivation. Safe Home remains outside the graph because it is a host operation;
the deterministic route carries it out only after a successful compound graph
and disables Limited Graph on standalone Safe Home requests.

The Limited Graph engine itself required no topology, binding, loop, routing,
authorization, Provider-handover, or execution-budget change. Its 26-test
suite passed, and the complete Test Agent suite passed 461 tests and 27
subtests. The configured package-root suite passed 1,120 tests and 27 subtests,
plus 79 Rust tests; documentation, JSON, configuration, environment-isolation,
release Manager, and wheel-build checks passed. No live process or physical
action was started by this checkpoint.

## 2026-08-17: immediate child-result observation

The successful physical retest showed that graph execution and retained child
visual evidence were correct, but the host did not receive those visuals until
the complete graph result returned. The final tool translator could only
recover `node_results` after every downstream node had finished. This made a
SAM2 or VLM visual appear to have been produced at graph completion even when
an earlier child had created it.

`LimitedGraphRunner.run` now accepts an optional child-result observer. The
runner calls it for each completed child attempt only after the result has
passed its declared output schema, credential-like values have been redacted,
the bounded retained copy has been accepted, and `CHILD_ATTEMPT_COMPLETED` has
been traced. The observer receives a deep copy of that safe result plus the
node ID, child tool name, attempt number, and graph call context.

The observer is an output-side presentation hook, not a graph operation. It
cannot return a route, mutate retained state, authorize an action, change a
retry, extend a deadline, alter a budget, or supply credentials. Both
synchronous and asynchronous observers are supported. Any observer exception
is logged and suppressed, and graph execution continues with the same result
and terminal semantics.

The host adapter connects the runner hook to the broker's
`observe_child_result` method. A broker that does not publish presentation
events may retain the default no-op behavior. The final graph result still
contains its bounded `node_results`, so nonstreaming hosts and replay retain the
existing compatibility path.

Regression coverage proves that an observer runs before the graph call
returns, receives the validated/redacted child result, and cannot change a
successful graph result when it raises. No Provider, Manager runtime, robot
process, or physical action is started by these tests.

The final checkpoint passed all 28 Limited Graph engine tests. The integrating
Test Agent suite passed 463 tests and 27 subtests, and every modified Python
module compiled.

The complete repository checkpoint then passed 1,124 Python tests and 27
subtests, 79 Rust tests, all configured wheel builds, release Manager
compilation, configuration and environment-isolation checks, JSON parsing,
documentation integrity, and source-manifest refresh.
