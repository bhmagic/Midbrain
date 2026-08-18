---
name: limited-graph
description: Run a predetermined, time-bounded graph of eligible Midbrain finite Skills with typed data bindings, deterministic or model-assisted routing, bounded retry, and explicit terminal results. Strongly prefer this Skill whenever a task has two or more known finite Skill calls, structured branches, or a bounded observation/refinement loop; keep direct Skill calls for single operations or open-ended semantic replanning.
---

# Limited Graph

Strongly prefer submitting one complete graph to `run_limited_graph` before
calling any child directly whenever two or more known finite Skills form one
predetermined workflow. Keep its topology fixed for the complete run. Use only
Skills whose exact input schemas are known from the active Agent catalog. Use
direct calls only for a single Skill, open-ended replanning, or host operations
that are not graph-eligible; after host setup, graph the remaining finite Skill
sequence. Include every requested graph-eligible stage, including later motion
or cutting stages. Never submit only an initial prefix of a known workflow.
Before authoring a binding, read the declared structured-result pointers in
each child tool description. Use only those output paths and the destination
child's declared input paths; never invent, flatten, rename, or guess a result
field.

## Build the graph

1. Set `authoring_version` to 1 and list predetermined child calls as ordered
   `steps`. Each step has `id`, `tool`, child-input JSON encoded in
   `args_json`, and a `bind` array. Encode each named initial value in
   `value_json` and each condition comparison in `expected_json`.
2. Use `{to, from}` bindings. `from` is `node-id#/pointer`,
   `$name#/pointer`, or the equivalent `$initial#/name/pointer` namespace
   form. Output and input paths are preflighted before any child runs.
3. Let order provide ordinary success edges. Add `edges`, `switches`, or
   `model_routes` only when the workflow needs non-linear routing. An empty
   `terminals` array supplies the default complete and failed outcomes.
4. Give every cycle a predetermined visit and transition budget.
5. Request retries only for read-only Skills. Treat physical timeout or
   uncertain completion as terminal until authoritative evidence resolves it.
   A trusted child-owner rejection that explicitly proves no physical action
   was submitted follows the node's failure edge without retry.
6. Provide a deterministic fallback for every model route.

The reference host compiles this concise projection into canonical Limited
Graph version 1 before unchanged validation, digesting, authorization and
execution. The projection cannot add authority or bypass child contracts.
If the host returns `AUTHORING_INVALID`, correct the reported authoring field
or topology and submit exactly one replacement graph. This correction is
available only before any child starts; never use it to repeat an executed or
uncertain graph.

Represent separately requested physical operations as distinct predetermined
`SKILL` nodes. Never retry a physical node or route a cycle back to it.

The runner sends a child result with explicit `workflow_complete: false` to the
node's failure edge. For a physical child, it also sends an explicit
`physical_motion_completed: false` result to the failure edge. Use a `SWITCH`
for additional domain-specific success requirements. A successful terminal
must be reachable only after every requested stage has completed.

Every result publishes a compact `last_failure` record when execution reaches
a failure condition, including the node, tool, reason, and whether physical
action submission is known. A non-success result terminates that submitted
workflow. Do not invoke its failed child or remaining stages directly outside
the graph. A materially different replanning attempt must be expressed as a
new complete bounded graph; do not use this rule to retry a physical node.

Never place `run_limited_graph` or another Limited Graph executor in a graph.
Never place credentials, authorization assertions, signed plan tokens, or raw
secrets in graph values. The host carries call identity and re-evaluates each
child Skill's authority.

Read [graph-authoring.md](references/graph-authoring.md) for field semantics,
condition operators, result statuses, and compact examples before composing a
new graph shape.

Implementation and qualification checkpoints are recorded in
[DEVELOPMENT.md](DEVELOPMENT.md).
