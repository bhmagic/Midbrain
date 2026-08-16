# Limited Graph development record

This file records significant implementation checkpoints, validation evidence,
and known gaps while the Skill is under development. The normative behavior is
defined in `contracts/22_limited_skill_graph.md`; this record explains how the
current code reached that contract and where to investigate regressions.

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
