# Limited Graph visual loss, point-offset substitution, and Safe Home denials

Date: 2026-08-17

## Retained runs

| Agent run | Retained behavior |
| --- | --- |
| `e6ca539b-ca48-416e-b948-eb06b48b694a` | World-axis establishment, FoundationPose candidate review and activation, five-sample VLM refinement, and the requested post-calibration motions completed. Graph child results contained visual evidence, but the journal contained no `visual.evidence.created` event. |
| `0968b0a2-bb00-442d-a201-3a9cfcb23a89` | The Agent refused the requested physical sequence because Limited Graph bindings copy values and could not express the first slicing point plus world +Z 10 cm. |
| `6ee0c08e-a27c-4158-9c7f-daaa92c4f155` | Graph `63dca45efad24ec5afdbb02331c720cc` completed, but its reposition node bound `/plan/path/planned_retract_endpoint_world_m`. That is the slicer's retract endpoint, not 10 cm above `/plan/path/slice_begin_point_world_m`. The physical graph therefore completed a semantically wrong reposition. |
| `352f70b6-5c6a-49b1-913e-2ea9354386bf` | A compound scene, corner-motion, slicing, reposition, repeat-slicing, and Safe Home request was denied without a tool call because the deterministic compound route hid the Safe Home host operation. |
| `ef0091d7-2746-4507-b2c0-c87705db3eb1` | A standalone `safe home` request was denied without a tool call even though the full Agent driver had registered `execute_basic_safe_home`. |

## Visual-event finding

Limited Graph retained child results under `node_results`. The Agent event
translator inspected only a tool result's root `visual_evidence`, so SAM2,
FoundationPose, and VLM evidence produced inside graph children was ignored.
The SDK could also expose the graph FunctionTool result as a Python dictionary
representation rather than JSON text; the translator attempted only
`json.loads` and discarded that result before inspecting it.

The compound scene-and-slicing route did not expose
`inspect_arm_semantic_scene`. Its point-derivation child deliberately performs
its internal scene inspection with visual evidence disabled, so no SAM2 visual
was created on that route even before event projection.

## Point-offset finding

Limited Graph correctly enforced copy-only bindings. No installed Skill
accepted an upstream world point plus a typed metric offset. The first affected
run denied the sequence. The following run substituted a nearby declared
Slicing output that already contained a point, but that field represented the
planned retract endpoint. The graph engine neither selected that semantic
field nor performed coordinate arithmetic; the authored graph contained the
wrong source pointer.

## Safe Home finding

The compound deterministic route omitted `execute_basic_safe_home`, so the
routed Agent surface made the host operation genuinely unavailable. The broad
standalone surface did contain the tool, but no narrow Safe Home intent route
constrained the model to use it. A disconnected Basic Provider also raised an
unstructured exception instead of returning the typed lifecycle continuation
used elsewhere in the Agent.

## Implemented correction

- Decode bounded JSON or literal dictionary tool outputs, inspect root and
  graph-child visual evidence, sanitize every item, deduplicate evidence IDs,
  and publish up to 32 safe UI events.
- Include `inspect_arm_semantic_scene` as the first graph Skill after direct
  scene-policy and Provider setup so the SAM2 evidence is generated and
  retained before coordinate derivation.
- Add the read-only finite `offset_world_point` Skill. It verifies the source
  point's active world identity, converts the exact requested unit, rotates
  arm-base or controlled-effector offsets through Fabric when needed, and
  emits an authoritative target point without motion or contact authority.
- Publish the actual Slicing `/plan/workcell_binding/world_frame` structure so
  a graph can bind the first slicing point and its world-frame identity into
  the offset Skill.
- Require the compound route to bind
  `/plan/path/slice_begin_point_world_m` for a target relative to the first
  slicing point and explicitly distinguish it from the planned retract
  endpoint.
- Add an exact standalone Safe Home route, keep Limited Graph off that
  host-only surface, retain Safe Home after compound graph completion, and
  return a typed `robot_arm.rebot_dm` HOT continuation with capability
  `robot_arm.safe_home` when Basic is disconnected.

These changes preserve the existing graph-in-graph prohibition, execution
budgets, copy-only binding contract, Provider and Manager duties,
authentication transport, session authorization, per-child authorization,
and controller safety authority.

## Stopped-software validation checkpoint

- Focused visual, spatial, discovery, host-broker, Safe Home, and output-audit
  set: 102 tests passed.
- Complete Test Agent suite: 461 tests and 27 subtests passed.
- Limited Graph engine suite: 26 tests passed.
- Configured repository package-root suite: 1,120 tests and 27 subtests
  passed; Manager and Fabric contributed 79 passing Rust tests.
- Documentation integrity for 135 Markdown files, duplicate-key-rejecting
  parsing for 105 JSON files, configuration baselines, Python environment
  isolation, release Manager compilation, and all configured Python wheel
  builds passed.
- The catalog-schema preflight accepted the exact
  `slice_with_blade → offset_world_point → move_effector_to_world_point`
  binding chain before any child invocation.
- No Provider, Manager runtime, robot process, or physical action was started
  by these tests.

## Retest checkpoint and live visual delivery

The following physical retest was reviewed from the retained Agent journal and
SDK session database:

| Agent run | Observed result |
| --- | --- |
| `9386c96e-14e4-4be6-87d6-382889a57298` | World establishment, FoundationPose review and activation, arm raise, five-sample alignment refinement, and the requested forward motion finished. The refinement graph completed three transitions with two physical actions and no retries. |
| `5eefde4d-20b7-4cc1-b327-463cd1abec87` | The SAM2 scene, corner derivation, corner approach, arm-base-direction translation, first slice, exact active-world +Z 10 cm point offset, reposition, and second slice completed. The graph completed eight transitions, visited every node once, executed four physical actions, and performed no retries. |
| `b6a24868-f74e-4223-8add-d93ebbba369b` | The subsequent host-only Safe Home operation returned `SAFE_HOME_COMPLETED` with physical completion true. It correctly remained outside Limited Graph. |

The compound slicing graph preserved the intended data lineage. The point
offset source exactly matched the first slice's published
`/plan/path/slice_begin_point_world_m`; `offset_world_point` applied active-
world `[0, 0, 10]` centimetres; the absolute-world motion target matched that
derived result; and the second slice reused the requested absolute first-cut
start. No approval denial or graph-child rejection was recorded in these
three runs.

The FoundationPose establishment graph reached its declared failed terminal
because the candidate child returned `workflow_complete=false`. The Agent then
used the existing direct review-and-activation operation and continued
successfully. This checkpoint records that terminal-semantic caveat without
changing FoundationPose or weakening Limited Graph's explicit-incomplete-result
rule.

The visual complaint exposed two separate presentation defects:

1. The host could inspect child visuals only after `run_limited_graph`
   returned. In all three observed graph visual cases, the journal assigned the
   visual event the same timestamp as the final graph tool completion instead
   of the child completion that produced it.
2. Each chat turn stored one `visual_evidence` object and one viewer, so a
   later visual replaced an earlier FoundationPose, VLM, or SAM2 card.

Limited Graph now offers a presentation-only child-result observer. It is
called immediately after output-schema validation, credential redaction,
bounded retention, and the child-attempt trace event. The hosted Agent broker
projects only sanitized visual evidence through the active run event sink.
Observer failures are logged and ignored by the graph runner, so presentation
cannot alter graph routing, retries, authorization, limits, or terminal state.
The normal final graph-result projection remains as a compatibility fallback,
and the run sink deduplicates evidence IDs already delivered live.

Agent chat projection now retains an ordered, unique, bounded list of up to 32
visual evidence objects per turn while continuing to expose the last object in
the legacy `visual_evidence` field. The shared regular/developer chat component
creates one viewer card per evidence ID, updates existing cards in place, and
removes only entries that fall outside the bounded list. A stopped-software
browser harness rendered two distinct cards at the same time and reported two
visual-card elements with the expected titles.

These changes do not modify Skill selection, graph topology, graph-in-graph
prohibition, copy-only bindings, Fabric ownership, Provider lifecycle,
authentication, session authorization, physical-action authorization, or
controller safety authority. A Test Agent restart is required before the
running pages load the updated Python and JavaScript assets.

The stopped-software regression checkpoint passed 463 Test Agent tests and 27
subtests, 28 Limited Graph engine tests, and Python compilation for every
modified module. The browser-DOM check used static visual evidence only. No
Provider, Manager runtime, robot process, or physical action was started.

The complete repository validator subsequently passed 1,124 Python tests and
27 subtests, 79 Rust tests, every configured wheel build, release Manager
compilation, configuration and environment-isolation checks, 105 JSON files,
and documentation integrity for 135 Markdown files. Source integrity manifests
were refreshed from the final files.
