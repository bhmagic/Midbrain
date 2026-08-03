# Agent Run Journal Contract

Status: v0.2 local-diagnostics working draft.

## Purpose

The Agent run journal gives a robot-local process a durable record of the same
versioned, SDK-neutral Midbrain events that drive browser SSE. It supports
post-restart diagnosis and shared Agent chat presentation without making
provider-native traces, a browser session, or a live SSE subscriber the durable
compatibility boundary.

The journal is observation state. The read-only developer viewer at
`/dev/run-journal` cannot create, resume, approve, reject, cancel, steer,
delete, or authorize an Agent task, Provider transition, or physical operation.

## Retained hierarchy and boundary

The retained hierarchy is:

1. one Midbrain session identified by the Manager boot UUID;
2. Agent runs started while that Manager boot is active; and
3. the complete retained `midbrain.agent_event` v1 envelopes for each run.

A session is `ACTIVE` while its Manager boot identity is current and becomes
`CLOSED` when a different Manager boot is observed. Records migrated from the
pre-session schema are grouped under a `legacy` session marked `HISTORICAL`.
Restarting only the Agent browser service against the same Manager boot does
not create another session.

For the operator transcript, each run also retains its accepted user prompt,
selected Agent/reasoning/visual models, attachment count, and public terminal
answer. It does not retain attachment image bytes, credentials, private
chain-of-thought, raw tool arguments, or raw tool outputs. Public reasoning
summaries, sanitized approval events, retry outcomes, and validated
visual-evidence references may remain in the normalized event envelopes.

Provider-native traces remain separate diagnostic material. A retained record
cannot reconstruct or resume model execution and does not copy the Agents SDK
model-session database.

## Storage and bounded retention

The implementation uses SQLite in the ignored robot-local `test_agent/run`
directory. A single writer batches turn, event, status, and session updates in
WAL mode so token streaming does not perform one foreground commit per event.
The defaults retain at most 500 terminal runs, 2,048 events per run, and 30 days
of terminal history. They are bounded by:

- `AGENT_RUN_JOURNAL_MAX_RUNS`;
- `AGENT_RUN_JOURNAL_MAX_EVENTS_PER_RUN`; and
- `AGENT_RUN_JOURNAL_RETENTION_DAYS`.

Active runs are not removed by terminal-run count pruning. Empty closed
sessions are removed when pruning runs. When the Agent service shuts down or
starts against a database containing a nonterminal prior-process run, it marks
that record `INTERRUPTED`. This diagnostic status does not infer whether an
external physical operation completed.

The v2 schema migration adds session and public-transcript columns in place.
Existing v1 runs and events are preserved under the historical `legacy`
session.

## Observation APIs and viewer

The in-memory streaming registry remains the source for live SSE replay. The
journal exposes bounded read-only observation endpoints:

- `GET /api/run-journal/sessions` lists retained Midbrain sessions;
- `GET /api/run-journal/sessions/{session_id}` lists runs in one session;
- `GET /api/run-journal/runs/{run_id}` returns one run and its retained event
  envelopes; and
- `GET /api/chat-session` returns the safe conversation projection for the
  active Manager boot.

These endpoints are not transcript mutation, task replay, task resume,
deletion, or command APIs.

The developer viewer uses a 50/50 independently scrolling layout. On the left,
a collapsible Midbrain-session parent contains the familiar collapsible
per-run cards. Selecting a run opens its prompt, public outcome, metadata, and
events on the right. Event category is the first expansion layer and the exact
retained event envelope is the second. This keeps high-volume delta events
closed by default while preserving access to every event still present under
the configured retention bounds.

## Health and failure behavior

The ordinary read-only Agent status response reports whether the journal
writer is running, its bounded policy, active session identity, pending queue
depth, and latest writer error. It reports only the database filename, not its
absolute host path.

Journal availability does not grant authority and must never weaken
controller, Provider, lease, motion-inhibit, or emergency-stop enforcement. An
unavailable or unwritable journal reports degraded health but does not prevent
the Agent UI, SSE stream, or existing command path from operating. A
storage-failure policy can become fail-closed only through a future explicit
field-audit requirement.

## Not yet field-audit grade

This local diagnostic journal is durable across process and browser restarts,
but it is not yet an authenticated field-audit system. Before using it for
incident records or disconnected deployments, Midbrain still needs explicit
access control, encryption at rest, tamper evidence or signing, retention
holds, redaction, deletion and export policy, storage-failure alarms, and
robot-fleet synchronization. Until authentication is implemented, the viewer
and its APIs must remain on the loopback-bound development service.
