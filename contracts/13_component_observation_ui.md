# Component Observation UI Contract

Status: v0.1 advisory working draft.

## Purpose

This contract separates browser observation from Provider and Skill
implementation language. A Python Provider and a Rust replacement remain
compatible with the same Manager-hosted observation page when they preserve
their manifest, Manager heartbeat, capability-readiness, Fabric stream, and
schema contracts.

The Manager hosts the Midbrain system portal and component observation pages.
The portal is the primary operator entry point for observation, Agent access,
guarded developer escalation, and shutdown. Opening an observation page does
not start a Provider, invoke a Skill, acquire authority, change residency, or
issue a hardware command.

## UI roles

The established UI roles remain:

- `OBSERVATION`: read-only system state, data, provenance, and diagnostics.
- `AUTHORIZATION`: decision-specific approval or rejection that does not itself
  execute the approved physical action.
- `DEVELOPMENT`: direct component controls, calibration, experiments, and
  administrative workflows.

Ordinary observation pages must not expose mutation routes in their JavaScript.
They may link to a development-boundary confirmation page. Only that confirmed
boundary may request Provider activation or start an advertised development UI.

## Midbrain system UI behavior

Normal core startup launches only Manager and Fabric. Manager serves Midbrain
after its health gate succeeds. Provider autostart and the Agent UI remain
separate launcher options; the normal desktop `Start Midbrain.cmd` entrypoint
enables the idle Agent UI so the Developer Agent and run journal are available
from the portal.

Midbrain shows:

- Manager and Fabric health;
- configured Provider process, heartbeat, residency, health, and readiness;
- installed Skill availability, adapter readiness, current run, and last
  terminal run state;
- a separate observation link next to each component's liveness;
- Developer Agent and read-only run-journal entry points;
- a guarded whole-workspace shutdown entry point; and
- aggregate counts that help identify cold, stale, unhealthy, running, or
  unavailable components.

`IDLE` is a healthy ordinary state for a finite Skill. A terminal Skill result
is retained as last-run information instead of making the Skill appear to
remain live.

## Component manifest field

Providers and Skills may add a top-level `ui` object conforming to
`schemas/component_ui.v1.schema.json`.

The Manager-hosted observation descriptor is implementation-neutral.
Development metadata may advertise either:

- a browser URL;
- a path resolved relative to a running Provider's registered control URL; or
- a workspace-relative launch command for a separate local process.

A development surface that is launched as a separate local process should also
advertise a workspace-relative `stop_command`. Whole-workspace shutdown invokes
these stop commands before Provider shutdown.

Development metadata is descriptive. It does not grant permission, authority,
or process lifecycle rights.

## Development boundary

The observation page links to a Manager-hosted confirmation page rather than
directly to the development target. The confirmation states that the operator
is leaving the ordinary agent-mediated workflow and may gain administrative or
physical capabilities. For a cold Provider, confirmation explicitly requests
`HOT` through Manager before starting or opening the developer surface. For a
finite Skill, confirmation starts only its development UI; it does not execute
the Skill.

The confirmation is a user-experience boundary, not sufficient authentication.
Mutation endpoints retain their authorization, fencing, control-authority,
CSRF/origin, and Provider-side safety requirements.

The main Midbrain page may expose a separate whole-workspace shutdown
confirmation. Its confirmed action delegates to the existing dependency-aware
`platform_core/scripts/stop_workspace.ps1` supervisor rather than issuing
independent process kills.

Agent lifecycle and physical-action approvals must present a concise
human-readable action, target, effect, and risk. Raw SDK tool-call JSON may be
retained for diagnostics but must not be the primary operator confirmation.

## Observation data

Component pages may display:

- instance and boot identity;
- process and residency state;
- heartbeat age and expiry;
- per-capability readiness;
- published Fabric streams, sequence, age, freshness, and schema;
- latest observation payloads or large-payload references;
- current or last finite-Skill state; and
- structured Provider or Skill diagnostics.

Large image, depth, point-cloud, and tensor payloads continue to follow the
shared-memory contract. The browser page must use a bounded visualization
adapter and must not copy unbounded payloads through Manager or Fabric.

## Rust migration

Hardware Providers are expected to be early Rust-migration candidates because
they own timing-sensitive SDK and device boundaries. A Rust replacement passes
the same observation UI without UI-specific compatibility code when it:

- registers the same Manager identity or declared replacement identity;
- reports the same lifecycle and capability semantics;
- publishes compatible Fabric schemas and timestamps;
- preserves shared-memory generation and lease behavior; and
- passes replay, fault, timing, lifecycle, and guarded hardware validation.

The UI is therefore a migration comparison surface, not an implementation
dependency.
