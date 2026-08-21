# Agent Event Stream Contract

Status: v0.1 compatibility working draft.

## Purpose

This contract separates Midbrain browser and observer integrations from any
specific agent SDK. Agent runtimes, finite Skills, mission supervisors, and
robot components may produce implementation-specific signals. An adapter
projects the subset needed by observers into versioned Midbrain events.

The event contract is the durable architectural boundary. Server-Sent Events
is only the current browser transport and grants no control authority.

## Compatibility boundary

The browser must not depend on OpenAI Agents SDK Python classes, raw Responses
API event objects, or provider-specific event names. The initial adapter maps
OpenAI events into the Midbrain event subset. Later OpenAI SDK versions, other
agent frameworks, deterministic workflows, and Google or local model runtimes
may implement additional adapters without changing ordinary browser behavior.

The Developer Agent uses only the canonical streaming-run family. It starts a
backend-owned run and returns a run ID plus a separate replayable event URL.
Losing the browser connection does not cancel or restart the run. The read-only
run journal projects the same observer event contract without adding a tool
catalog or authorization path.

Model transport and observer transport are separate choices. A model adapter
may stream through Responses, a provider's Chat Completions-compatible API, or
a future native provider runtime, while the browser still receives the same
Midbrain events. The term `ChatCompletions` in an internal adapter names an API
dialect; it does not authorize a synchronous browser route. Model-specific
tool-discovery features must be adapted before the model request and must not
change this event contract.

Native GPT Responses Tool Search and non-GPT client-executed compatibility
search both project as `tool.search.called` and `tool.search.completed`.
Rejected client compatibility arguments project as `tool.search.failed` with
only a bounded error code, retryability, parse location, input length, and
count diagnostics; raw search arguments, arbitrary rejected names, messages,
and loaded definitions are not published to browser observers. A compatibility
completion or failure is recognized only from a matching call ID and an exact
client `tool_search_output` or `tool_search_error` envelope, because a Chat
Completions function-output item does not retain the originating tool name.

## Version 1 envelope

Every event contains:

- `schema`: `midbrain.agent_event`.
- `schema_version`: currently `1`.
- `event_id`: stable within the retained run history.
- `sequence`: monotonically increasing within one run.
- `occurred_at`: UTC timestamp.
- `run_id`: backend-owned run identity.
- `source`: adapter identity, initially `openai_agents`.
- `type`: stable Midbrain event type.
- `payload`: type-specific JSON object.

The JSON schema is `schemas/agent_event.v1.schema.json`.

## Initial event subset

The initial implementation publishes:

- `run.started`, `run.completed`, and `run.failed`.
- `assistant.message.delta`.
- `assistant.reasoning_summary.delta` and completion metadata.
- `agent.updated` and agent handoff lifecycle events.
- `tool.called`, `tool.completed`, and called/completed/failed tool-search
  lifecycle events.
- `approval.required` and `approval.resolved` for unresolved host policy
  decisions.
- `visual.evidence.created` for a validated image reference and structured
  annotations conforming to the visual-evidence contract.
- `skill.retry.recovered` and `skill.retry.exhausted` for a strictly validated,
  capture-only observation retry that declares fresh-evidence requirements and
  confirms that no physical action was submitted.
- MCP tool-list and MCP approval lifecycle events when supplied by the SDK.

This is deliberately a small UI subset. It is not a complete event-sourcing
model for robot execution. Camera artifacts, annotations, motion dispatch,
controller completion, verification, broader retry scheduling/attempt starts,
and safety stops should be added as domain events when their producers are
integrated. The current capture retry events are emitted from the completed
finite-Skill result, so they report recovery or exhaustion rather than serving
as a live retry command channel.

## Privacy boundary

The ordinary browser event projection excludes raw tool arguments, raw tool
outputs, prompts, credentials, and private reasoning. It may expose the SDK's
user-facing reasoning summary deltas. Provider-native traces may be retained
separately under a restricted diagnostic policy.

## Authorization and autonomy

An SDK approval interruption is a development interaction mechanism, not a
permanent requirement that a human click every action. Before invoking a
protected tool, the host evaluates the active authorization policy. An exact
operation within that policy proceeds without an SDK interruption. An
unresolved operation may still produce `approval.required` for development.

Policy authorization does not replace deterministic safety. Controller
limits, preview freshness, calibration provenance, collision checks, fencing,
leases, motion inhibit, dead-man behavior, and emergency stops remain outside
the model and remain authoritative.

SSE is not a command channel, robot bus, safety channel, or execution lease.
User prompts and approval decisions use separate HTTP commands. Future mission
commands or active steering must likewise use an authenticated command path and
must be recorded as events after acceptance.

## Replay and retention

The live implementation retains a bounded in-memory sequence and supports SSE
reconnection from `Last-Event-ID` or an explicit sequence. The same normalized
events are now also written to the bounded robot-local diagnostic journal
defined in `19_agent_run_journal.md`. Browser SSE still reads the in-memory
channel; journal storage does not change live transport behavior.

The local journal survives process restarts but is not yet an authenticated,
encrypted, tamper-evident field-audit system. Disconnected field deployment
still requires the additional controls listed in the journal contract.
