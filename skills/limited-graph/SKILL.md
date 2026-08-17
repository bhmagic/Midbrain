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

1. Select a finite start node and at least one terminal node.
2. Use `SKILL` nodes for child calls, `SWITCH` nodes for structured conditions,
   `MODEL_ROUTE` nodes only when deterministic routing is insufficient, and
   `TERMINAL` nodes for every final outcome.
3. Encode literal child arguments as JSON in `arguments_json`. Bind prior
   values with declared JSON pointers instead of repeating or interpreting
   them. Output and input pointer paths are preflighted before any child runs.
4. Give every cycle a predetermined visit and transition budget.
5. Request retries only for read-only Skills. Treat physical timeout or
   uncertain completion as terminal until authoritative evidence resolves it.
6. Provide a deterministic fallback for every model route.

Represent separately requested physical operations as distinct predetermined
`SKILL` nodes. Never retry a physical node or route a cycle back to it.

The runner sends a child result with explicit `workflow_complete: false` to the
node's failure edge. For a physical child, it also sends an explicit
`physical_motion_completed: false` result to the failure edge. Use a `SWITCH`
for additional domain-specific success requirements. A successful terminal
must be reachable only after every requested stage has completed.

Never place `run_limited_graph` or another Limited Graph executor in a graph.
Never place credentials, authorization assertions, signed plan tokens, or raw
secrets in graph values. The host carries call identity and re-evaluates each
child Skill's authority.

Read [graph-authoring.md](references/graph-authoring.md) for field semantics,
condition operators, result statuses, and compact examples before composing a
new graph shape.

Implementation and qualification checkpoints are recorded in
[DEVELOPMENT.md](DEVELOPMENT.md).
