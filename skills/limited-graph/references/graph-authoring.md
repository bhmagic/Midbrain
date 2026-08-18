# Limited Graph authoring reference

## Contents

- Graph envelope
- Reference-host concise projection
- Node behavior
- Data bindings
- Conditions and retry
- Limits
- Result interpretation

## Graph envelope

The immutable execution contract uses `schema_version` 1, a stable
human-readable `name`, one `start_node`, complete `nodes`, `initial_values`, and
execution limits. Programmatic graph hosts may submit that canonical shape.
The reference Test Agent exposes the smaller authoring projection below and
compiles it to this canonical shape before validation or execution.

Every node object contains `skill`, `switch`, `model_route`, and `terminal`.
Set exactly one matching the node `kind` to an object and set the other three
to null. This explicit shape keeps the Agent tool schema strict.

## Reference-host concise projection

Set `authoring_version` to 1 and put finite child calls in ordered `steps`.
Each step contains `id`, `tool`, child-input JSON encoded in `args_json`, and a
`bind` array. Encode each named initial value in `value_json`; a text value
must include its JSON quotes. Encode each condition comparison in
`expected_json`. A binding uses `{to, from}`. `to` is a child-input JSON
pointer; `from` is `node-id#/pointer` for a prior compact result or
`$name#/pointer` for a named initial JSON value. The reference host also
accepts the equivalent namespace form `$initial#/name/pointer`; for example,
`$request#` and `$initial#/request` both select the root of initial value
`request`.

Step order supplies the ordinary success edges. The final step goes to
`complete`, and each failure goes to `failed`. Add an `edges` record only to
override those defaults. Add `retries`, `switches`, and `model_routes` only
when the workflow needs them. An empty `terminals` array creates bounded
default `complete` and `failed` terminal nodes; explicit terminal records
retain a workflow-specific status and message.

The concise limits map to the canonical limits as follows:

| Authoring field | Canonical field |
| --- | --- |
| `seconds` | `max_active_runtime_s` |
| `transitions` | `max_transitions` |
| `visits` | `max_visits_per_node` |
| `model_routes` | `max_model_routes` |
| `physical_actions` | `max_physical_actions` |
| `result_bytes` | `max_retained_result_bytes` |

Compilation is deterministic and grants no authority. The host validates the
concise input, compiles it, then applies the unchanged canonical schema,
eligible-child, compact-pointer, reachability, retry, cycle, limit,
authorization, digest and execution checks. The canonical graph is the only
graph used by the runner.

If concise compilation or canonical static preflight rejects the graph before
the runner creates a graph run or starts a child, the reference host returns a
normal Limited Graph result with `status` equal to `AUTHORING_INVALID`, zero
transitions and zero physical actions. Correct the reported field or topology
and submit exactly one replacement graph in that Agent run. A second authoring
rejection terminates the run. Runtime data errors, authorization stops,
timeouts, uncertain outcomes and completed child calls never use this
authoring-correction path.

## Node behavior

- `SKILL` invokes one eligible manifest-discovered finite Skill. Put literal
  arguments in `arguments_json`, apply bindings, and validate the assembled
  object against the child Skill schema. `next_node` follows a completed call.
  `failure_node` follows an exception, invalid bound arguments, or exhausted
  retry condition. Authorization is a stopped outcome for the complete graph
  invocation rather than an ordinary failure edge.
- `SWITCH` evaluates ordered cases against one initial value or completed node
  result and follows the first match. It follows `default_target` if none
  match.
- `MODEL_ROUTE` sends only its declared inputs and candidate edge descriptions
  to one host-configured routing profile. The router may select only a listed
  `edge_id`. Invalid, unavailable, timed-out, or low-confidence output follows
  `fallback_target`.
- `TERMINAL` ends with its declared status and message.

The runner sends an explicit `workflow_complete: false` result to
`failure_node`. For a physical child, it also sends an explicit
`physical_motion_completed: false` result to `failure_node`. These common
completion fields cannot silently follow `next_node`. Route through a `SWITCH`
when a child has additional domain-specific success requirements, such as an
exact `status` or `goal_reached` value. Never infer physical completion from a
normal FunctionTool return.

Include every requested graph-eligible stage in the submitted topology. Do not
submit only a prefix of a known workflow and leave later motion, contact, or
cutting stages for direct calls. A successful terminal must be reachable only
after every requested stage has completed.

The default host registers `FAST_TEXT` for a no-tools, one-turn structured
router using the configured fast text model. It registers `FAST_VISION` for
host-stored `midbrain.visual_evidence` references; the graph carries the
evidence ID and channel metadata, never image bytes or model credentials.
Additional local-model profiles may be registered by host code through the same
callback interface. A graph cannot provide a model name, endpoint, executable,
or API key. Disable a default profile in host configuration when its data must
not leave the machine. An unavailable or malformed profile result always takes
the declared fallback.

## Data bindings

A binding writes one selected JSON value into the child argument object.
`target_pointer` is a JSON pointer into that object. Select the source with
`source_kind`:

- `INITIAL` uses `source_name` and ignores `source_node_id`.
- `NODE_RESULT` uses `source_node_id` and ignores `source_name`.

Use `source_pointer` to select a nested value. An empty pointer selects the
complete source. Missing sources, malformed pointers, and attempts to replace
the child argument root with a non-object are invalid.

Every installed Skill declares an output schema in discovery metadata. Child
tool descriptions list its stable structured-result pointers. A
`NODE_RESULT` source pointer must be one of those declared paths, and a target
pointer must exist in the destination input schema. The runner checks both
before executing the first node. Do not move a nested field to the result root
or derive a new name from prose. For example, Slicing publishes
`/plan/path/slice_begin_point_world_m` and
`/plan/path/planned_retract_endpoint_world_m`; it does not publish
`/outward_retract_end_position_world_m`.

An output property can be optional because the Skill has multiple status
variants. Follow the node's failure edge for an incomplete result and use a
`SWITCH` when a declared field exists only under a particular successful
status. Preflight confirms the path is declared, while runtime still fails
closed if that optional value is absent.

## Conditions and retry

Conditions support `EQ`, `NE`, `LT`, `LTE`, `GT`, `GTE`, `IN`, `EXISTS`, and
`TRUTHY`. Decode `expected_json` as the comparison value; use null only for
operators that do not consume it.

`max_attempts` includes the first call. A `retry_condition` means the returned
structured result is unacceptable and should be tried again. Only read-only
Skills may set `max_attempts` above one in contract version 1. Adapter
exceptions may also retry for read-only Skills until the same limit is
exhausted. Stateful and physical Skills are never inferred to be safe to
repeat. Separately requested physical operations may be represented by
distinct predetermined `SKILL` nodes, but no cycle may route back to a physical
node and no physical node may have more than one attempt.

A physical child exception remains `UNKNOWN_OUTCOME` unless the installed
child owner emits the trusted host signal that no physical action was
submitted. That explicit pre-submission rejection follows `failure_node`,
decrements the graph's submitted physical-action count, and does not retry.
The graph never derives this signal from exception text, status prose, HTTP
codes, or timing.

## Limits

Every graph supplies maximum active runtime, transitions, visits per node,
model routes, physical child calls, and retained result bytes. Repository host
ceilings may lower these values. Hitting the first limit returns
`LIMIT_EXHAUSTED`; it never extends the graph automatically.

## Result interpretation

Treat `COMPLETED` and `COMPLETED_WITH_RETRIES` as successful terminal runs.
Treat `AUTHORIZATION_REQUIRED`, `UNKNOWN_OUTCOME`, `LIMIT_EXHAUSTED`,
`MODEL_ROUTE_UNAVAILABLE`, and `FAILED` as explicit incomplete outcomes.
Use compact `last_failure` for the most recent failure kind, node, child tool,
reason, and known physical-submission state. Inspect full `trace` explicitly
through the detail reference when more evidence is required. Also inspect
`terminal_node`, `limit`, and `last_completed_node` rather than inferring
completion from the final child payload alone.

A non-success result ends ownership of that submitted workflow. Do not invoke
the failed child or any remaining graph stage directly afterward. Express a
materially different replan as a new complete bounded graph. This is not
permission to retry a physical node.
