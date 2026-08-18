# Runtime catalog and Skill result-tier feasibility

Date: 2026-08-17

Status: feasibility audit followed by the approved implementation checkpoint.
The measurements and proposal analysis below retain their historical wording;
the implementation disposition is recorded separately so design assumptions
remain distinguishable from shipped behavior.

## 2026-08-17 implementation disposition

The two approved proposals are implemented without changing authentication,
Manager lifecycle authority, Provider duties, Fabric ownership, controller
leases, or physical authorization:

- Manager now serves `GET /v1/agent-runtime-catalog`, retaining every Provider
  and capability entry with regulated lifecycle/readiness fields, and
  `GET /v1/providers/{id}/detail` for one complete current `ProviderView`.
- The Agent exposes `inspect_midbrain_runtime` for the compact complete catalog
  and top-level-only `inspect_provider_detail` for sanitized full or selected
  Provider evidence.
- Agent Skill discovery schema version 3 requires the
  `x-midbrain-result-tiers` annotation. All 23 installed manifests were
  source-audited and migrated; 22 use `HOST_SANITIZED_REFERENCE`, while the
  undiscoverable empty-result FoundationPose primitive explicitly uses `NONE`.
- The common FunctionTool boundary validates each complete result, sanitizes
  it, retains it in bounded session-scoped SQLite storage, and returns only the
  Skill's selected compact pointers plus an opaque detail reference. Both
  special prepared-motion wrappers use the same finalizer.
- The Agent exposes top-level-only `inspect_skill_result_detail` for an exact
  prior result ID and optional JSON pointer, including an explicit complete
  sanitized-result request.
- Limited Graph preflight allows result bindings, retry conditions, switches,
  and model routes only through the source Skill's compact tier. Runtime rejects
  leaked noncompact fields, retains compact node results, and projects the
  graph result itself through its compact tier.

The detailed stores and inspection operations are diagnostic and
non-authoritative. Storage unavailability is returned in `detail_ref` and does
not change an outcome or trigger a retry. Detail inspection is excluded from
Limited Graph child descriptors.

## Decisions in scope

The operator accepted these design directions for feasibility analysis:

- retain every configured Provider and every advertised capability in the
  Agent runtime catalog;
- replace each complete Provider configuration and arbitrary heartbeat detail
  object with a regulated lifecycle/readiness record;
- preserve access to more detailed Provider evidence through an explicit
  read-only request;
- require every installed Skill output contract to declare a compact tier and
  a detailed tier; and
- return the compact tier to the Agent and Limited Graph by default so the
  same projection also reduces subsequent model-session history.

In this document, “full Provider and capability catalog” means complete
coverage of catalog entries. It does not mean copying every launch argument,
environment key, controller diagnostic, or arbitrary Provider heartbeat field
into every model call.

## Conclusion

Both proposals are feasible within the existing duty boundaries.

The runtime-catalog proposal is high-feasibility and low-to-medium risk.
Manager already owns every lifecycle and capability fact needed by the
regulated projection. The main work is a new Manager-owned response shape, an
Agent client method, replacement of the current complete-copy projection, and
an on-demand detail tool.

The mandatory two-tier Skill-result proposal is also high-feasibility, but is
medium-to-high implementation risk because it touches the common Skill return
boundary, Limited Graph validation and retention, visual/result observers,
Provider handover continuations, and model-session history. Most Skill
implementations do not need to change if the host performs the projection,
but all 23 installed manifests require a source-backed compact-field audit.

## Proposal A: complete compact runtime catalog

### Current path

`GET /v1/providers` returns the complete `ProviderView`: full process
configuration, process state, and the complete Provider heartbeat report with
arbitrary `details`. `GET /v1/capabilities` returns a much smaller regulated
capability record. The Agent's `build_midbrain_runtime_snapshot` currently
copies both responses and only redacts credential-like environment values.

This makes the current runtime tool a complete diagnostic dump rather than a
regulated decision surface. The latest retained runtime message used for this
feasibility measurement contained eight Providers, 46 capabilities, and 18
eligible Skills.

### Proposed default shape

The default catalog retains all Provider entries with:

- Provider ID and display name;
- configured dependency IDs;
- process state;
- residency, health, ready, and expired state;
- active Provider instance and boot identity;
- last-seen/freshness information; and
- bounded last-error, Manager-error, or blocking-prerequisite information only
  when present.

It retains all capability entries with capability name, Provider ID, active
Provider instance identity, and availability. Provider-level lifecycle fields
need not be repeated on all 46 capability rows because each row references its
Provider record.

The default catalog excludes launch command, arguments, working directory,
environment key names, control URLs, process IDs, stop-policy configuration,
arbitrary heartbeat diagnostics, controller targets, controller telemetry,
and complete Provider-native reports.

### Measured size

The retained runtime object measured 95,973 characters when serialized as
compact JSON. Stored in the SDK session envelope it occupied 108,808
characters because the result is itself an encoded tool-output string.

| Runtime representation | Entries retained | Compact JSON size | Reduction from current object |
| --- | --- | ---: | ---: |
| Current complete-copy snapshot | 8 Providers, 46 capabilities | 95,973 characters | — |
| Compact Providers, existing capability rows unchanged | 8 Providers, 46 capabilities | 16,511 characters | 82.8% |
| Compact Providers, normalized complete capability rows | 8 Providers, 46 capabilities | 11,429 characters | 88.1% |

These are projections of a real retained snapshot, not synthetic entry
counts. The final contract may be slightly larger after schema/version fields
and explicit error variants are added. It should remain close to one tenth of
the present size while preserving complete Provider and capability coverage.

### On-demand detail

The existing complete Manager Provider view remains available to host code. A
read-only Agent tool can request one exact Provider ID and one bounded detail
section. It should return sanitized current Manager evidence, not a cached
runtime-summary copy. Requests should support an explicit section or JSON
pointer and a response-size limit so asking for one error does not reproduce
the entire catalog.

The top-level Agent must also be allowed to request the complete sanitized
detail for that one Provider. “Complete” means every field permitted by the
Provider-detail observation contract; it never includes credentials, bearer
material, signed authorization, private control handles, or other prohibited
host state. A full request deliberately spends more model context and should
be explicit rather than the default.

The detail tool does not start, stop, warm, bind, authorize, or invoke a
Provider. Existing lifecycle and physical-control tools remain the only
command paths.

### Placement of Provider detail access

Provider detail access is not a finite Skill and is not another Resource
Provider. The live data remains Manager-owned. Manager exposes a versioned
read-only observation endpoint, and the Agent host exposes that endpoint to
the model as a host FunctionTool such as `inspect_provider_detail`.

A host FunctionTool is an SDK command surface; it does not have to be a
Midbrain Skill. This tool belongs beside `inspect_midbrain_runtime`, not in the
installed Skill catalog, Provider lifecycle, capability binding, or Limited
Graph child registry.

The compact Provider catalog should advertise a Provider-detail schema ID and
either a bounded list of available sections or a schema-reference operation.
Manager's regulated fields already have known names. Arbitrary Provider
heartbeat `details` do not currently have one universal schema, so fields
inside that object are discoverable only when the Provider publishes a
registered detail schema or when the Agent requests the complete sanitized
record.

### Required implementation surfaces

1. Add a Manager-owned versioned compact catalog structure and endpoint.
2. Derive it from the same Provider reports and capability map already used by
   Manager lifecycle and binding logic.
3. Add Agent client methods for the compact catalog and exact Provider detail.
4. Replace `inspect_midbrain_runtime` output and its instructions with the
   compact complete catalog.
5. Add a read-only bounded Provider-detail tool.
6. Test stopped, starting, warm, hot/not-ready, hot/ready, unhealthy, expired,
   superseded-instance, missing-capability, and blocking-prerequisite variants.

No Provider manifest or heartbeat schema must change for the first version.

## Proposal B: mandatory two-tier Skill results

### Schema feasibility

All 23 installed Skill manifests already carry a discovery-version-2
`output_schema`, and the repository has a source-backed audit for each schema.
The current discovery meta-schema permits additional JSON Schema keywords
inside `output_schema`. Draft 2020-12 validation also accepts an
`x-midbrain-result-tiers` annotation and ignores it during ordinary instance
validation. A local validation probe confirmed that a result still validates
normally when this annotation is present.

Therefore, the manifest-facing change can be one mandatory annotation inside
each output schema. The annotation should contain at least:

- its own annotation version;
- the exact compact JSON pointers;
- the detailed-result handling mode;
- whether a detailed result is expected for success, failure, or both; and
- a maximum public compact-result size.

The annotation cannot be only a `compact: true` boolean. The host needs the
exact field selection to preserve Skill-specific graph bindings and failure
semantics.

Making the annotation mandatory changes discovery-version-2 semantics even
though generic JSON Schema libraries accept the keyword. A new discovery
contract version is the cleanest compatibility boundary. Extending version 2
in place is technically possible but would make old and new version-2
manifests mean different things.

### Required runtime behavior

The common host return boundary must perform these operations in order:

1. normalize the adapter's complete result;
2. validate the complete result against the existing output schema;
3. sanitize credential-like material before any result is retained or exposed;
4. store the sanitized detailed result in bounded diagnostic storage;
5. project the mandatory compact pointers and append an opaque detail
   reference with digest, byte size, schema identity, and availability;
6. validate the compact projection against the tier annotation; and
7. return only the compact projection to the FunctionTool caller.

The existing `output_schema` remains the complete adapter-result validation
contract. The custom annotation defines the Agent/graph projection. Generic
JSON Schema validation will not enforce the annotation, so discovery and host
code must validate its syntax, pointer reachability, required common outcome
fields, size limit, and projection result.

### Limited Graph behavior

Limited Graph must receive and retain compact child results. Every binding,
switch condition, retry condition, and model-route input must be restricted to
the compact pointer set during graph preflight. A field needed by a graph must
be deliberately promoted into that Skill's compact tier; the graph must not
fetch or bind through detailed diagnostics.

The graph runner can then use compact child results for routing and node
bindings. Child detail references remain available for operator or developer
inspection, but do not grant execution, authorization, or routing authority.

Limited Graph is itself an installed Skill and must use the same two-tier
contract. Its compact result should retain graph identity and digest, terminal
status/node, completion message, counts, compact node results or explicitly
published terminal outputs, and one graph-detail reference. Its detailed
result can retain the complete trace and detailed node references.

### Measured size on the current two-slice graph

The latest eight-Skill-node graph was projected using all common outcome and
safety fields plus every JSON pointer actually consumed by a later graph
binding. A simulated detail reference was added to every child. This is a
feasibility lower bound, not the final per-Skill selection; useful Agent
summary fields will add some size.

| Result representation | Compact JSON size | Reduction |
| --- | ---: | ---: |
| Eight complete child results | 77,311 characters | — |
| Common outcome fields, all actual binding fields, and detail references | 4,426 characters | 94.3% |
| Complete graph result | 122,023 characters | — |
| Graph summary with those compact child results and graph detail reference | 5,233 characters | 95.7% |

The actual stored SDK graph-output item occupied 128,575 characters. Returning
the compact graph result at the FunctionTool boundary therefore removes most
of that item from the model session. Direct Skill calls receive the same
benefit because their compact result becomes the SDK function-call output.

### Model-session history effect

The Agents SDK session currently retains FunctionTool outputs. The optional
history limit avoids splitting a model/tool turn but does not summarize tool
results, and the active reference configuration uses an unlimited SQLite
session history. Therefore:

- a compact direct Skill result reduces the next model call in the same run;
- that compact result is also what later user turns read from the session;
- graph children are not separate root Agent turns, but compacting them
  reduces graph retention and the final graph result;
- compacting Limited Graph itself removes the full graph output from all later
  model calls; and
- old stored sessions remain unchanged, while new results use the compact
  boundary.

This is the expected one-change/two-benefit behavior: smaller immediate tool
responses and smaller accumulated run history.

### Detailed-result storage and retrieval

The existing Agent run journal explicitly excludes raw tool outputs, so it
must not silently become the detailed-result store. A separate bounded local
diagnostic store is the least disruptive first implementation. It should use
opaque result IDs, Manager-boot and Agent-session scoping, content digests,
per-result byte limits, total retention limits, and expiration.

Data already designed for Fabric remains in Fabric. Visual, depth, point
cloud, tensor, transform, and canonical world-state payloads should remain
references rather than being copied into detailed Skill JSON. Provider-native
diagnostics remain Provider evidence when their owning contract says so. The
diagnostic Skill-result record may retain the Provider result or reference
needed to reconstruct the finite Skill outcome, but it must not create a
second canonical state owner.

An Agent-facing read-only detail tool should accept only an opaque detail
reference plus an optional bounded JSON pointer or named section. It should
return an index/summary by default and retrieve a large section only when
explicitly requested. Retrieved detail will naturally enter the current model
history, so selective retrieval remains important.

The top-level Agent must also be allowed to request the complete sanitized
Skill output associated with the opaque reference. A full request does not
make detailed fields graph-bindable and does not change the Skill's completion
or physical outcome. It is diagnostic observation of one already completed
invocation.

### Placement of Skill-result detail access

Skill-result detail access is likewise not a classical Skill or Provider. The
Skill produces the result, but the Agent host validates, sanitizes, stores, and
projects the result at the FunctionTool boundary. The bounded diagnostic
result store is therefore the observation source, and the Agent host exposes a
host FunctionTool such as `inspect_skill_result_detail`.

The lookup key must be the exact opaque detail reference returned by that
invocation, not merely a Skill name. This prevents the Agent from accidentally
reading a stale or unrelated invocation. The reference binds result ID,
schema/Skill identity, digest, Manager boot, Agent session, byte size, and
retention state.

The full Skill output schema remains discoverable metadata even though only
compact values are returned by default. The Agent can therefore see permitted
field names without receiving their values. The tier annotation separately
identifies which of those fields are compact and graph-bindable. With deferred
tool loading, the full pointer catalog need only be exposed after that Skill
is selected rather than for all Skills in every prompt.

Neither detail-inspection FunctionTool should be registered as a Limited Graph
child. Otherwise a graph could bypass the compact binding contract and feed
arbitrary diagnostics back into routing. A developer or ordinary top-level
Agent may inspect details explicitly, subject to its read permission and
response bound.

Because a complete detail response becomes a FunctionTool output, it will
consume context in the current diagnostic turn. A later session-history
projection should replace that completed-turn payload with the detail
reference and a retrieval receipt; otherwise one explicit diagnostic lookup
would remain in every future model call and partially recreate the bloat that
the two-tier system removed.

### Cross-cutting consumers that must be preserved

The compact audit for every Skill must retain or internally consume before
projection:

- `status`, workflow completion, and physical action requested/submitted/
  completed fields used for fail-closed graph routing;
- Provider lifecycle continuations needed by the existing handover broker;
- required next-tool continuations needed by direct Agent operation;
- every declared graph-binding, switch, retry, and model-route field;
- visual-evidence references needed by live graph child presentation;
- current frame, epoch, calibration, and Provider binding identities when they
  are required to interpret a returned coordinate or physical outcome; and
- unknown-outcome evidence when complete validation fails after a physical
  child may have run.

Signed authorization, host-private continuation state, credentials, and
control leases must not become retrievable diagnostic content merely because a
full result is stored.

### Failure behavior

A projection or diagnostic-storage failure after physical execution must not
rewrite the known physical outcome as success or failure. The compact result
should preserve the validated outcome and report detailed-result availability
separately. If the complete result itself cannot be validated after a physical
action may have run, the existing unknown-outcome rule remains authoritative.

Diagnostic storage should be operationally non-authoritative. An unavailable
detail store may report degraded observability, but must not cause a Skill to
repeat a physical action or make Manager, Provider, lease, or safety behavior
depend on diagnostic persistence.

## Migration scope

### Shared infrastructure

- discovery annotation parser and validator;
- compact-pointer projector and compact-result validator;
- bounded sanitized detailed-result store;
- top-level read-only Provider-detail and Skill-result-detail FunctionTools,
  including explicit complete-detail requests, and developer inspection APIs;
- common result finalizer used by ordinary and manually registered Skill
  FunctionTools;
- Limited Graph descriptor, preflight, result retention, and terminal
  projection changes; and
- session/history regression measurements.

### Per-Skill work

All 23 installed manifests need a source-backed compact-pointer selection. The
22 discoverable Skills require direct-call and graph validation. The
undiscoverable nested FoundationPose primitive currently publishes an empty
direct result contract; it can declare that no detailed result is available
until it has a direct Agent adapter.

Most Skill implementation code should not change because the common host can
validate, store, and project its current result. Manually registered wrappers
for prepared physical motion and other special Agent tools must call the same
finalizer instead of returning JSON directly.

### Suggested implementation order

1. Write the proposed contract changes and exact ownership rules for approval.
2. Implement the annotation parser, projector, and in-memory test detail store.
3. Qualify one read-only Skill through direct invocation and Limited Graph.
4. Add the bounded persistent diagnostic store and detail retrieval surface.
5. Add Limited Graph compact-child enforcement and compact graph output.
6. Audit and annotate all 23 installed Skill manifests.
7. Route every special/manual Skill wrapper through the common finalizer.
8. Implement the compact complete Manager catalog and Provider detail tool.
9. Run schema, direct-Skill, graph, authorization, visual, history, journal,
   and retained-size regression suites before a physical retest.

## Principal risks and mitigations

| Risk | Consequence | Required mitigation |
| --- | --- | --- |
| A needed graph field is omitted | Static graph rejection or missing runtime binding | Require compact-pointer preflight and source-backed tests for every current graph field |
| Completion/safety fields are omitted | Incorrect success, failure, or retry route | Mandatory common outcome pointer policy with physical variants tested fail-closed |
| Provider handover continuation is projected too early | Graph cannot activate and resume a dependency | Consume continuation before public projection or make the minimal continuation compact |
| Visual evidence is omitted | Child visuals disappear again | Observe validated raw evidence before projection and retain compact evidence references |
| Detailed storage copies credentials or authority | Security and duty-boundary violation | Sanitize before retention and prohibit authorization/lease/private continuation fields |
| Detail-store write fails after motion | Outcome may be misreported or action repeated | Keep result outcome authoritative and report detail availability separately |
| Full detail retrieval recreates history bloat | Large subsequent model calls | Default to an index and require bounded section selection |
| Detail tools are exposed as graph children | Graph routing can bypass the compact contract | Keep both tools top-level-only and exclude them from hosted child descriptors |
| Discovery version is changed silently | Old installations have ambiguous semantics | Use an explicit new contract version or a separately negotiated extension version |

## Acceptance criteria

The proposals are ready for implementation only after the contract change is
approved. Implementation is acceptable when:

- all configured Providers and all advertised capabilities remain present in
  the default runtime catalog;
- the default catalog excludes arbitrary Provider diagnostics and is at least
  75% smaller than the retained complete-copy baseline;
- exact Provider detail remains retrievable through a read-only bounded path;
- the top-level Agent can explicitly retrieve one complete sanitized Provider
  record without making the operation a Skill or lifecycle command;
- every installed Skill declares a validated tier annotation;
- all current Limited Graph bindings resolve only through compact pointers;
- direct and graph Skill calls return only compact results to the SDK;
- the current two-slice graph result is at least 80% smaller without losing
  completion, safety, binding, visual, or Provider-handover behavior;
- detailed results remain retrievable when storage is healthy;
- the top-level Agent can explicitly retrieve the complete sanitized output of
  one exact Skill invocation by opaque detail reference, while Limited Graph
  cannot invoke either detail tool;
- unavailable diagnostic storage cannot authorize, repeat, or change the
  reported outcome of a physical action; and
- authentication, Manager authority, Provider safety, Fabric ownership,
  leases, and physical authorization remain unchanged.
