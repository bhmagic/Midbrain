# Agent Chat History Projection

Status: v0.2 robot-local session working draft.

## Purpose

This contract defines the operator-facing projection of Agent runs into a
scrollable conversation. It does not make a browser tab a robot event store, a
model-session authority, or a command channel.

Each run started from the regular page or developer view creates one user
message and one Agent message. The Agent message may contain the final answer,
visual evidence, and an expandable execution summary derived from the stable
Midbrain Agent event contract.

## Execution summary boundary

The expandable summary may contain:

- public reasoning-summary text emitted by a compatible model runtime;
- sanitized Agent and tool lifecycle labels;
- approval lifecycle labels;
- bounded observation-retry outcomes; and
- the time of each projected update.

It must not contain private chain-of-thought, raw tool arguments, raw tool
outputs, credentials, or provider-native diagnostics. A runtime that does not
supply public reasoning summaries still produces a useful execution summary
from Midbrain lifecycle events.

## Midbrain-session retention

One conversation session corresponds to one Manager boot identity. Manager
publishes a boot UUID from `/health`; the Agent service records it as the parent
of every run started during that Midbrain boot. If Manager identity is briefly
unavailable during Agent startup, the Agent uses a process-scoped fallback and
adopts the Manager boot UUID once Manager becomes reachable. A later Manager
restart creates a new session rather than joining runs across different
physical-system epochs.

The bounded robot-local SQLite journal stores the accepted user prompt, model
selection metadata, attachment count, public terminal Agent answer, and safe
normalized events. It does not copy attached image bytes into the transcript.
Validated visual-evidence references and annotations remain subject to their
separate backend artifact retention.

`GET /api/chat-session` projects at most the latest 40 runs from the active
Manager boot into `midbrain.agent_chat_turn.v1` records. Both the regular and
developer pages hydrate from this same endpoint when opened and poll it while
open. Closing or reopening either tab therefore does not create or erase a
conversation. A tab that owns a live SSE run keeps its local active turn while
other tabs observe the journaled progress. Once the run is terminal, every tab
converges on the same server projection.

Polling reconciles turns by run identity and updates existing elements in
place. It must not replace the entire transcript, move a live local turn out of
chronological order, force-scroll an operator who is reading older content, or
close an expanded execution/event disclosure. Unchanged terminal projections
are skipped so periodic synchronization does not create visible flicker.

There is no clear-history UI. A Manager restart establishes the next
conversation parent; older sessions remain available in the read-only run
journal until bounded retention removes their runs.

## Durability and authority boundary

Robot-local session history is durable development diagnostics, not a field
audit, task-resume, or model-session reconstruction mechanism. The SQLite
journal and browser projection cannot cancel, interrupt, approve, reject,
resume, steer, or authorize a run. The Agents SDK model-session database remains
a separate runtime concern.

The service is not yet authenticated, encrypted, or tamper-evident. Until the
security work described by `19_agent_run_journal.md` is complete, the chat and
journal observation APIs must remain on the loopback-bound development
service.
