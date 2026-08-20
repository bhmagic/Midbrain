# Limited Graph Status and Qualification

This document records the current reference-implementation status of Limited
Graph. The normative composition, discovery, authorization, and result rules
remain in the
[Limited Skill Graph Contract](../contracts/22_limited_skill_graph.md) and the
[Agent Skill Discovery Contract](../contracts/11_agent_skill_discovery.md).

## Current status

The reference implementation is accepted as **near stable for continued
development use** at implementation checkpoint `8777ebf`, merged into `main`
by `32c90d1`. Near stable means that the intended complete linear workflow has
passed retained live forward tests and the repository's stopped-software
validation, while specifically named branch, presentation, sensing, and
context-size qualification remains open.

This status is not a robot-safety certification, a guarantee that every graph
topology is qualified, or permission to bypass a child Skill's normal
authorization. A graph is one bounded finite Skill; it does not become a new
authority owner, Provider, controller, Fabric producer, or task-success
sensor.

## Implemented reference behavior

| Area | Current behavior |
|---|---|
| Agent authoring | The Agent receives a concise ordered-step projection. The host deterministically compiles it into canonical immutable graph version 1 before validation or execution. |
| Static preflight | Child eligibility, typed inputs, compact result pointers, conditions, retries, switches, routes, reachability, limits, and nested-graph exclusion are checked before a child starts. |
| Skill results | Every installed Skill publishes a complete result schema and an explicit compact/detail policy. Direct and graph calls validate the complete result, retain a sanitized bounded detail copy when configured, and expose compact values plus an opaque detail reference. |
| Runtime context | The normal Agent runtime view contains every Provider and capability but only regulated lifecycle and readiness fields. One complete sanitized Provider record or exact prior Skill result can be requested explicitly by the top-level Agent. Detail tools are not graph children. |
| Provider handover | A child may return its own declared typed Provider-residency continuation. The existing host lifecycle broker performs that continuation under unchanged Manager policy and resumes the same child call. The graph does not select a Provider or mint authority. |
| Physical children | Each physical child uses the same exact preview, authorization, call identity, fencing, freshness, completion, and uncertain-outcome rules as the corresponding direct call. |
| Failure visibility | Terminal compact results publish `last_failure` so the next Agent decision can distinguish authoring, preflight, child, binding, routing, timeout, and limit failures without loading the full trace. |
| Visual evidence | Validated child visual evidence is forwarded to the Agent event stream as each child produces it. Graph completion is not required before the UI can receive the visual. |
| Correction | One authoring/static-preflight correction may occur only before a graph has started. A started graph or physical action is never automatically resubmitted by that correction path. |
| Bounds | Wall time, transitions, physical actions, retry counts, per-node timeout, and graph topology remain explicitly bounded. Nested Limited Graph calls are prohibited. |

## Duty and authority boundary

The checkpoint did not move responsibility or authentication data between
components:

| Owner | Responsibility retained |
|---|---|
| Agent | Interpret the objective, author the graph, evaluate compact results, and make any later semantic decision. |
| Limited Graph | Validate and execute the submitted bounded topology, copy declared compact values, route typed outcomes, and report its trace and terminal state. |
| Skill | Own task semantics, inputs, result truth, deterministic internal work, and any declared Provider continuation. |
| Manager | Own Provider lifecycle, readiness, resource coordination, authority policy, and regulated runtime/detail views. |
| Fabric | Own timestamped shared observations, transforms, provenance, and expiry; it does not host Agent conversation history or authorize motion. |
| Provider/controller | Own hardware or compute behavior, final command validation, fencing, completion, and safe-state behavior. |
| Agent host | Broker discovery, sanitization, detail retention, lifecycle requests, child calls, events, and exact authorization envelopes without exposing credentials to the graph. |

Credentials, signed action material, leases, private continuation state, and
arbitrary Provider diagnostics do not enter graph bindings, compact results,
traces, or model-visible authoring arguments.

## Accepted evidence boundary

The retained live checkpoint completed the intended scene inspection, Fabric
corner derivation, absolute-world transit, direction translation, two Slicing
submissions, intermediate offset motion, child-declared Integrated Provider
handover, incremental SAM2 evidence, and separate Basic safe home. A separate
failure run verified that a trusted Slicing pre-submission rejection remained
inside graph ownership and appeared through compact `last_failure` without
executing later nodes.

The exact run identifiers, timing comparison, payload measurements, and test
counts are retained in the
[Limited Graph speed audit](performance/2026-08-17-limited-graph-speed-audit.md),
the [Reference Agent development record](../test_agent/DEVELOPMENT.md), and
the [Limited Graph development record](../skills/limited-graph/DEVELOPMENT.md).
Evergreen architecture and operator documents intentionally do not duplicate
those mutable measurements.

## Qualification still open

The near-stable checkpoint does not yet claim retained live qualification for:

- a purpose-built success/failure branch, switch, retry, or model-selected
  route covering each supported routing form;
- multiple child visuals arriving close together and remaining independently
  inspectable in the Developer Agent and run journal;
- task-level sensing that proves material was physically cut rather than only
  proving that the Slicing motion plan completed;
- a strict byte or token bound for the complete projected Agent session; or
- a narrower compact projection for large nested
  `refine_arm_root_translation` sample and visual structures.

These gaps must remain visible in the
[Current Limitations and Roadmap](09_LIMITATIONS_AND_ROADMAP.md). They do not
invalidate the accepted linear success path, but they prevent describing
Limited Graph as fully qualified.

## Operator interpretation

The Developer Agent and run journal observe the same backend run. Child
visuals should appear when produced and remain associated with their exact
run and child call. Closing a page does not stop the run. **Stop task** cancels
the selected Agent run and its owned subtasks while leaving background
Providers under Manager control; it does not prove the physical outcome of an
already submitted action.

A completed FunctionTool transport call is not automatically a successful
Skill, graph, or physical task. Inspect the graph terminal status,
`last_completed_node`, `last_failure`, exhausted limit, compact child outcome
fields, physical-completion fields, and required post-action evidence. Request
full Skill detail only when those compact fields are insufficient for the next
top-level Agent decision.

## Failure investigation record

Retain the following sanitized identifiers before changing code or retrying:

- implementation commit and local configuration revision;
- Agent run ID, graph run ID, graph digest, and root tool-call ID;
- child node ID, child call ID, Skill ID, and Provider boot identity;
- terminal status, last completed node, compact `last_failure`, and exhausted
  limit;
- ordered trace timestamps and exact compact result pointers used by bindings
  or conditions;
- visual-evidence references and relevant Fabric frame, epoch, calibration,
  observation, and transform revisions; and
- measured physical state and terminal safe state when motion was submitted.

Never add credentials, signed action tokens, lease material, private model
reasoning, or unrestricted raw Provider configuration to this record.
