# Limited Graph authoring reference

## Contents

- Graph envelope
- Node behavior
- Data bindings
- Conditions and retry
- Limits
- Result interpretation

## Graph envelope

Set `schema_version` to `1`, provide a stable human-readable `name`, identify
one `start_node`, and declare all nodes and execution limits before invocation.
`initial_values` is a list of named JSON values. Encode each value in
`value_json`; duplicate names are invalid.

Every node object contains `skill`, `switch`, `model_route`, and `terminal`.
Set exactly one matching the node `kind` to an object and set the other three
to null. This explicit shape keeps the Agent tool schema strict.

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

## Limits

Every graph supplies maximum active runtime, transitions, visits per node,
model routes, physical child calls, and retained result bytes. Repository host
ceilings may lower these values. Hitting the first limit returns
`LIMIT_EXHAUSTED`; it never extends the graph automatically.

## Result interpretation

Treat `COMPLETED` and `COMPLETED_WITH_RETRIES` as successful terminal runs.
Treat `AUTHORIZATION_REQUIRED`, `UNKNOWN_OUTCOME`, `LIMIT_EXHAUSTED`,
`MODEL_ROUTE_UNAVAILABLE`, and `FAILED` as explicit incomplete outcomes.
Inspect `trace`, `terminal_node`, `limit`, and `last_completed_node` rather than
inferring completion from the final child payload alone.
