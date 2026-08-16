# Limited Skill Graph Contract

Status: v0.1 development contract; stopped-software implementation required
before physical promotion

## 1. Purpose and boundary

Limited Graph is one finite Midbrain Skill that executes a complete,
predetermined graph of other eligible finite Skills. It reduces Agent/model
round trips for known sequences, typed branches, and bounded refinement loops
without turning the graph into a privileged Provider or a source of physical
authority.

The graph Skill owns orchestration state. Each child Skill continues to own its
domain validation, Provider binding, evidence policy, authorization,
controller interaction, result, and cleanup. A graph invocation never converts
the union of child permissions into blanket authority.

When deterministic intent routing narrows the active Agent tool surface, the
graph's child catalog is intersected with that exact routed surface. The graph
cannot use the host's wider baseline allowlist to bypass a task-specific route.
Routes exposing at least two finite FunctionTools strongly prefer Limited Graph
before any child is called directly; single-operation routes continue to use
direct calls. Necessary non-Skill host setup may remain direct, after which the
remaining finite Skill sequence strongly prefers Limited Graph. A compound
request must expose the union of graph-eligible child Skills required by all of
its predetermined stages; routing must not make a complete graph impossible by
selecting only an initial intent prefix.

## 2. Immutable graph

The complete graph is validated before its first node executes. Validation
normalizes the graph, computes one canonical SHA-256 digest, and rejects:

- duplicate, missing, unreachable, or incorrectly typed nodes;
- missing targets or a missing terminal path;
- an unknown, disabled, ineligible, or manual-only child Skill;
- the Limited Graph tool or another graph executor as a child;
- malformed JSON arguments, bindings, conditions, or model routes;
- a cycle without effective transition and visit bounds; and
- a retry request that exceeds the child's allowed side-effect semantics.

No node or edge may be added, removed, or changed after execution begins.
Changing graph content creates a different digest and a new run.

## 3. Node and edge semantics

Contract version 1 executes one node at a time and supports:

- `SKILL`: invoke one eligible finite Skill with schema-validated arguments;
- `SWITCH`: evaluate ordered deterministic cases over structured JSON;
- `MODEL_ROUTE`: ask one configured routing profile to select only a declared
  edge; and
- `TERMINAL`: end with one explicit status and message.

Loops use ordinary edges back to an earlier node. Parallel child invocation,
runtime node generation, arbitrary code expressions, and nested graphs are not
part of version 1.

An explicit child result with `workflow_complete` equal to false follows the
SKILL node's failure edge. An explicit physical-child result with
`physical_motion_completed` equal to false also follows the failure edge. A
normal FunctionTool return cannot silently override these completion fields.
Graphs use a following `SWITCH` for additional domain-specific result rules.

## 4. Typed data binding

Literal Skill arguments are parsed from `arguments_json`. A binding copies one
JSON value from a named initial value or completed node result to a declared
JSON pointer in the argument object. Bindings do not execute expressions,
format strings, scripts, or model interpretation. The complete assembled
object is validated against the child Skill's active discovery schema
immediately before every attempt.

The runner may normalize a child JSON string into structured JSON, but it must
retain a digest and bounded trace sufficient to identify what was routed. It
must not insert credentials or host-private canonical continuation state into
the graph value store.

## 5. Identity, authentication, and authorization carry

The host creates one root graph call identity and a unique child call identity
for every node attempt. Host-local lineage binds at least graph run, graph
digest, node, attempt, parent call, child call, session principal, and active
deadline.

Identity is carried; authority is re-evaluated. The host applies the same
child input validation, eligibility, approval policy, timeout, and prepared
action binding used by a direct Agent call. A child requiring authorization
must not execute merely because the graph tool was selected or approved.

API keys, cookies, passwords, HMAC secrets, authorization assertions, and raw
signed physical-control tokens are prohibited from graph inputs, node values,
model prompts, traces, and results. Host-owned opaque references may be used
only inside the owning authorization or prepared-action implementation.

## 6. Retry and uncertain outcomes

`max_attempts` includes the first invocation. Contract version 1 permits more
than one attempt only for a child whose discovery safety class is `READ_ONLY`.
A later child contract may declare stronger idempotency semantics, but the
graph must not infer them from an error message or HTTP status.

A timeout, process loss, cancellation, or transport failure during a physical
or otherwise consequential Skill does not prove the action failed to occur.
The runner returns `UNKNOWN_OUTCOME` or another owner-provided incomplete
result and does not repeat the action. A retry that later succeeds remains
visible as `COMPLETED_WITH_RETRIES`.

Distinct predetermined nodes may represent separately requested physical
operations. No retry or cycle may invoke the same physical node again.

## 7. Execution limits

Every graph declares maximum active runtime, transitions, visits per node,
model-route calls, physical child invocations, and retained result bytes. The
host applies equal or lower ceilings. Hitting the first ceiling ends the graph
with `LIMIT_EXHAUSTED`, the exact limit name, and the latest completed trace.
Limits are never increased automatically.

## 8. Model routing

A model route receives only its declared structured inputs, instruction, and
candidate edge identifiers/descriptions. The host resolves a configured
routing profile; Agent-supplied backend URLs, credentials, and arbitrary model
identifiers are prohibited.

The router result contains an edge identifier, confidence, and bounded
provenance. The runner follows the deterministic fallback when the profile is
unavailable, times out, returns malformed output, selects an unknown edge, or
falls below the declared confidence threshold. A router may not create a node,
change arguments or limits, or grant physical authority.

The host profile receives graph lineage and deadline metadata but not the root
authorization/session object. Model input is limited to the node's instruction,
declared structured values, and declared route descriptions.

The reference host exposes fixed `FAST_TEXT` and `FAST_VISION` profile names.
The text profile is a one-turn, no-tools structured-output Agent. The vision
profile resolves only bounded host-stored visual-evidence references and sends
their selected channels through the configured VLM router. A local backend may
be registered under another fixed profile using the same callback contract;
backend selection remains host configuration, never graph data.

## 9. Results and observability

The final result reports the graph run ID and digest, terminal status and node,
transition and visit counts, active runtime, retry count, selected edges,
bounded per-attempt results or hashes, authorization interruptions, exhausted
limit, and last completed node. Inner child calls must remain observable even
though they are not separate Agent turns.

At minimum the runtime emits or journals node start, node completion, retry,
edge selection, model fallback, authorization rejection, limit exhaustion,
unknown outcome, and terminal completion. Observability does not authorize an
action and must redact credential-like material.

A child FunctionTool returning normally means only that its invocation produced
a result. It does not make a domain-specific operation successful. A graph must
inspect the child's typed completion/status fields before following a successful
terminal path; it must never translate transport completion into physical or
workflow completion. The runtime emits `CHILD_RESULT_INCOMPLETE` when an
explicit common completion field sends a child result to its failure edge.

## 10. Promotion and current limits

Promotion proceeds through deterministic unit tests, read-only real-Skill
composition, stateful no-motion composition, simulated physical invocation,
and developer physical qualification. Physical children are eligible without
a separate Limited Graph boolean gate. Exact routed-surface eligibility,
prepared-action binding, per-child authorization, physical-action budgets,
timeout handling, and unknown-outcome handling remain mandatory.

Version 1 does not promise parallel nodes, nested graphs, automatic restart
resume, automatic authorization resume, or generic dynamic model installation.
An authorization requirement ends the current invocation before the child
action; a later graph run must rebuild fresh evidence unless a separately
versioned opaque-resume contract is implemented.
