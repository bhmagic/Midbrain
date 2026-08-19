# Agent Skill Discovery Contract

Status: v0.3 mandatory two-tier discovery contract.

## Purpose

This contract defines the concise metadata exposed to an OpenAI Agents SDK
agent before a finite Midbrain Skill is selected. It does not define provider
selection and does not grant physical-control authority.

The discovery boundary follows the Agents SDK tool model:

- The agent initially sees each deferred Skill's stable tool name and exact
  FunctionTool description, while its parameter schema remains unloaded.
- The model semantically selects an eligible Skill from those descriptions.
- The selected Skill's original complete FunctionTool definition becomes
  callable after search, including its parameter schema and the structured-
  result pointers carried by its description.
- Nondeferred tools remain callable immediately. In the Reference Agent this
  includes `run_limited_graph`.
- A user or test policy may explicitly require tool use or narrow the eligible
  tool set.
- Deterministic guardrails, authorization, Manager binding, and provider-side
  safety checks remain outside semantic selection.

Deferred loading is a model-adapter optimization, not a requirement of the
Skill contract. A host must publish only discovery features supported by the
selected model adapter. A `gpt-*` model receives the original deferred
`FunctionTool` definitions plus exactly one native `ToolSearchTool`; this is
the unchanged OpenAI Responses hosted-search path. Every non-`gpt-*` model
receives the client-executed compatibility path. That path exposes an ordinary
`tool_search` FunctionTool whose exact deferred names and descriptions are
visible first and whose `paths` select from the already eligible functions. Its
result carries `type=tool_search_output`, `execution=client`, the matching call
ID, `status=completed`, and the selected original complete FunctionTool
definitions. SDK run-local dynamic enablement makes those functions callable
on the following model turn.

The compatibility path reproduces the OpenAI client-executed two-response
contract. A Chat Completions backend cannot emit native Responses
`tool_search_call` or `tool_search_output` items, continue through hosted search
inside the same response, or guarantee Responses context-end injection and
cache behavior. Midbrain normalizes the ordinary compatibility function events
to the canonical observer event names but does not claim those unavailable
transport features. Search does not invoke a Skill, add routing policy, create
another catalog, persist selection into a fresh run, or grant authority.
Materialization changes context size and discovery mechanics only; it does not
change eligibility, arguments, result validation, authorization, or execution
ownership.

Official references:

- <https://developers.openai.com/api/docs/guides/tools-tool-search>
- <https://openai.github.io/openai-agents-python/tools/>

## Manifest field

Discoverable Skills add an `agent_discovery` object to their existing
`manifest.json`.

Required fields:

- `schema_version`: currently `3`.
- `discoverable`: whether normal agents may offer this Skill.
- `tool_name`: stable snake-case function-tool name.
- `description`: concise statement of what the Skill does and when to use it.
- `when_to_use`: positive routing examples.
- `when_not_to_use`: important negative routing examples.
- `side_effects`: short machine-visible descriptions of external effects.
- `safety_class`: one of `READ_ONLY`, `STATEFUL_NO_MOTION`,
  `PHYSICAL_MOTION_AUTHORIZATION_REQUIRED`, or `MANUAL_ONLY`.
- `expected_latency`: one of `LOW`, `MEDIUM`, `HIGH`, or `UNKNOWN`.
- `required_permissions`: semantic permissions required before execution.
- `input_schema`: strict JSON object schema exposed to the Agents SDK.
- `output_schema`: self-contained JSON Schema for the normalized agent-visible
  complete result. The root is an object, explicitly declared `properties`
  publish the complete field-name catalog, and the required
  `x-midbrain-result-tiers` annotation declares the smaller stable composition
  surface. An open `additionalProperties` value may retain diagnostics in the
  complete result, but it does not make undeclared fields compact or valid
  graph-binding targets.
- `execution_adapter`: stable adapter ID and adapter kind used only after
  selection. Discovery never imports or starts the adapter.

A non-discoverable Skill also provides `disabled_reason`. It remains available
for explicit local development workflows but is excluded from normal automatic
selection.

Every installed Skill manifest, including non-discoverable and manual-only
Skills, must carry discovery schema version 3 and an `output_schema`. This
keeps installation, catalog inspection, direct invocation, replay tooling, and
bounded graph composition on one result contract instead of creating a
graph-specific registry.

The `x-midbrain-result-tiers` annotation has exactly four fields:

- `schema_version`: currently `1`;
- `compact_pointers`: unique declared JSON pointers returned to the Agent by
  default and available to Limited Graph bindings, conditions, and routes;
- `detail_policy`: `HOST_SANITIZED_REFERENCE` for a complete result retained
  by the Agent host, or `NONE` only for an empty direct-result contract; and
- `max_compact_bytes`: a per-Skill UTF-8 bound that includes the opaque detail
  reference.

When declared by the complete schema, common status, message, workflow,
physical-action, task-success, required-next-tool, and visual-evidence fields
must remain compact. Each Skill must also retain the frame, epoch, calibration,
coordinate, and outcome fields required by its existing graph consumers. A
compact pointer does not create a field or change the field's owner.

The output schema is metadata, not authority. It must not contain credentials,
signed actions, host-private continuation state, or claims that a physical
operation is authorized. Result validation does not replace the Skill's domain
checks, Manager binding, host authorization, or Provider-side safety policy.

## Complete validation, compact projection, and detail observation

The host normalizes a JSON-text result to JSON and validates the complete raw
result against the selected Skill's output schema before projection. A schema
mismatch is never reported as a successful Skill result. If a physical child
has already been invoked and its complete result cannot be validated, a
bounded orchestrator treats the physical outcome as unknown rather than
selecting an ordinary success or retry edge.

After complete-result validation, the Agent host removes credential-like and
authorization-like values, stores the sanitized complete result in bounded
session-scoped diagnostic storage when the policy requests it, and projects
only the selected compact pointers. The normal FunctionTool result contains
that compact object plus an opaque `detail_ref`. A storage failure is reported
in the reference and does not change a Skill outcome or authorize, retry, or
repeat an action.

If an unexpectedly large selected value would cross `max_compact_bytes`, the
host preserves bounded outcome and physical-state fields plus the detail
reference, omits only values that cannot fit, and adds a
`midbrain.compact_result_projection` marker naming or counting omissions. The
marker is diagnostic, is not graph-bindable, and cannot cause an action retry.

Only paths reachable through declared `properties` and array item schemas may
be selected as compact pointers. Limited Graph may bind, switch, retry, or
route only through a selected compact pointer or its declared descendants.
The empty JSON pointer does not expose the complete result to a graph. External
references and dynamically named properties are not part of discovery schema
version 3.

An optional property can still be absent in a particular failure variant.
Callers must follow the Skill's completion status and branch before consuming a
success-only field. Runtime binding remains fail-closed when an optional value
is absent.

The complete output schema and its field names remain visible in the selected
FunctionTool description even though detailed values are omitted by default.
When compact values are insufficient, the top-level Agent may call
`inspect_skill_result_detail` with the exact opaque result ID and either one
JSON pointer or a null pointer for the complete sanitized output. The operation
is a host diagnostic observation FunctionTool, not a Skill, Provider,
lifecycle command, or authority source. It is not offered as a Limited Graph
child, and retrieved detail does not become graph-bindable.

The bounded detail record belongs to the Agent host as a diagnostic copy. It
does not replace Fabric-hosted observations, Manager state, Provider state,
Skill-owned persistent artifacts, controller state, or signed-action stores.
Credential material, raw authorization assertions, control leases, and
host-private continuation state must not be retained merely to make a complete
result inspectable.

## Initial evaluation policy

Initial evaluation may use both strong agent instructions and
`ModelSettings(tool_choice="required")` to ensure that the model selects one of
the small eligible tool set. The SDK resets tool choice after a tool call by
default, preventing a forced-tool loop.

This is a test policy, not a permanent routing rule. After multi-Skill routing
evaluations pass, normal operation should use `tool_choice="auto"` or deferred
tool search where appropriate.

## Provider boundary

The selected Skill declares semantic capabilities. It must not ask the model to
choose a physical provider instance.

The Manager resolves capabilities deterministically and returns an advisory
binding containing provider instance and boot identity. During migration, a
caller may provide an explicit provider ID only as a fallback. Existing direct
provider-ID request routes remain compatible until binding enforcement is
explicitly enabled.

## Execution adapter boundary

Discovery and execution are separate registries. The catalog scans manifests
without importing Skill code. An agent host applies an explicit eligible-tool
allowlist, verifies that every offered descriptor has a registered adapter,
and then constructs an Agents SDK `FunctionTool` using the manifest's name,
description, and strict input schema.

Selecting a tool resolves its adapter ID; it does not resolve a provider.
The adapter invokes the finite Skill, which requests a capability binding from
Manager. A configured explicit provider ID remains a compatibility fallback
and must be retained in the Skill result and audit provenance.

Stateful or physical adapters must pass through an explicit host authorization
policy. SDK approval hooks may project an unresolved policy decision into an
early development interaction, but a bounded autonomous policy may resolve an
exact eligible operation before an SDK interruption is created. Provider-side
authorization and deterministic safety checks remain authoritative. A
model-selected tool call never grants physical authority, and the absence of a
human dialog never implies that safety checks were bypassed.

## Provider activation readiness

Lifecycle command acceptance and dependency readiness are distinct states. An
Agent host that activates a cold finite-Skill dependency must not describe a
`HOT` request as complete until current Manager evidence shows that the exact
Provider is `HOT`, ready, and unexpired. When the Skill result names a
`required_capability`, the host also waits for Manager to advertise that exact
capability as available from the selected Provider.

Hosts should define dependency `HOT` to include process startup when needed.
If a model nevertheless selects process-only `START` while supplying a
non-null required capability, the host must not immediately return the model to
the finite Skill. It either waits for the Provider to naturally reach `HOT` and
publish that capability, or returns a typed `HOT` continuation. Plain `START`
without a capability may remain a process-only development operation.

The wait is bounded and reports either `READY` or `TIMED_OUT` with the latest
sanitized readiness evidence. `READY` instructs the model to invoke the
original finite Skill immediately in the same run, without another runtime
inspection or duplicate lifecycle call. A timeout never grants authority and
must not be reported as successful activation. Skill adapters may retain a
separate bounded data-plane check for a frame or observation that races the
control-plane heartbeat.

When the selected Provider declares dependencies, the host submits one `HOT`
request for that task-facing Provider. Manager owns transitive dependency
ordering and deduplication. Agent-visible recovery data may identify those
dependencies for diagnosis, but the continuation names only the task-facing
Provider; the model must not reproduce the dependency graph as separate
lifecycle calls.

For preview-then-approve execution, the continuation should contain only an
opaque pending preview identifier when the host retains canonical state. The
host resolves the full motion envelope for authorization and execution. A
model-copied target, timing value, transform, or controller digest is not an
authority source and must not replace the pending host record.

An Agent adapter may project one nonphysical preparation and its exact
execution continuation as one agent-visible prepared action. This projection
is valid only when all of the following remain true:

- preparation itself grants no physical authority and submits no motion;
- the Skill result names one allowlisted continuation with a minimal opaque
  identifier;
- host state binds the preparation, authorization evidence, and execution to
  one exact SDK or adapter call identity rather than to model-generated input;
- the existing authorization policy is evaluated against host-recovered
  canonical state after preparation and before execution;
- approval or bounded autonomous policy authorizes that exact prepared action,
  not a class of later actions;
- execution revalidates freshness, controller state, and authority and fails
  closed when the call-scoped preparation is missing or changed; and
- a nonmatching continuation, dependency request, calibration step, replan,
  user question, or other semantic branch is returned without automatic
  execution.

This is a host projection, not a general instruction to recursively follow
`required_next_tool`. Different Skills use that field for lifecycle recovery,
reviewed activation, re-observation, replanning, and physical commit. An
adapter must explicitly bind each eligible pair and leave all other
continuations visible to the Agent or owning deterministic workflow.
