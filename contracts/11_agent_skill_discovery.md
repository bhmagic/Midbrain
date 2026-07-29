# Agent Skill Discovery Contract

Status: v0.1 advisory working draft.

## Purpose

This contract defines the concise metadata exposed to an OpenAI Agents SDK
agent before a finite Midbrain Skill is selected. It does not define provider
selection and does not grant physical-control authority.

The discovery boundary follows the Agents SDK tool model:

- The agent initially sees a stable tool name, a short description of what the
  Skill does and when to use it, and the tool input schema.
- The model semantically selects an eligible Skill from those descriptions.
- Complete Skill instructions and implementation-specific resources are loaded
  only after selection when deferred tool loading is used.
- A user or test policy may explicitly require tool use or narrow the eligible
  tool set.
- Deterministic guardrails, authorization, Manager binding, and provider-side
  safety checks remain outside semantic selection.

Official reference: <https://openai.github.io/openai-agents-python/tools/>

## Manifest field

Discoverable Skills add an `agent_discovery` object to their existing
`manifest.json`.

Required fields:

- `schema_version`: currently `1`.
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
- `execution_adapter`: stable adapter ID and adapter kind used only after
  selection. Discovery never imports or starts the adapter.

A non-discoverable Skill also provides `disabled_reason`. It remains available
for explicit local development workflows but is excluded from normal automatic
selection.

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

Stateful or physical adapters must use SDK approval hooks as an early user
interaction layer, but provider-side authorization and safety checks remain
authoritative. A model-selected tool call never grants physical authority.
