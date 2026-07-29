# Phase 1 Systemic Housecleaning Implementation

Date: 2026-07-26
Status: implemented and software-tested in non-enforcing shadow mode

## Safety boundary

No Phase 1 change grants new physical authority. Existing Integrated Engage +
LB gating, the fenced Basic lease, motion inhibit, and the proven safe
termination helper remain authoritative. No physical arm or camera test was
performed as part of this implementation.

The frozen reference is
`.reference_baselines/pre_phase1_20260726`. Runtime source and configuration do
not reference that directory.

## Implemented

### Agent Skill discovery

- Added a concise manifest descriptor compatible with OpenAI Agents SDK
  function-tool name/description selection.
- Added local catalog scanning that reads metadata without importing or
  starting Skills.
- Marked stationary workcell calibration discoverable.
- Marked the vegetable-cutting prototype local, manual-only, and excluded from
  automatic discovery.
- The initial test agent still offers only its narrow
  `identify_pointed_object` tool and uses required single-tool selection. The
  broader catalog is inspectable, but dynamic execution adapters for every
  discovered Skill are a later interconnection step.

### Advisory provider binding

- Added deterministic Manager capability binding with opaque binding IDs and
  provider instance/boot snapshots.
- A ready, healthy, HOT provider wins over a hard-coded provider ID.
- Explicit provider IDs remain compatibility fallbacks.
- Binding is advisory and does not start providers or change an existing data
  path.

### Direct controller path and audit copy

- Added direct `POST /v1/motion/plan` stage-and-preview at the Integrated
  controller.
- The endpoint is nonphysical and returns the normalized target and preview ID.
- All state-changing Integrated HTTP requests are recorded in an append-only
  provider-local audit before execution.
- Accepted and rejected outcomes are separate events.
- Fabric publication is asynchronous, cursor-backed, and replayable; Fabric
  latency is not in the synchronous controller path.

### Advisory Manager authority

- Added acquire, renew, release, expiry, explicit preemption, retained history,
  and fencing generations.
- Integrated polls the Manager view and compares it with the existing fenced
  Basic lease.
- Integrated does not enforce or replace authority from this Manager view in
  Phase 1.

### Manager-owned shutdown shadow

- Added an ordered Manager shutdown plan with motion relinquishment, Basic
  safe-state confirmation, ordinary provider stop, audit flush, Fabric stop,
  and Manager-last ordering.
- The plan is `SHADOW_DRY_RUN` and does not stop a process.
- Integrated requests the plan asynchronously when safe termination begins, so
  Manager delay or failure cannot delay the existing safe path.

### Orbbec RGB-D compatibility route

- Added Manager capability readiness and Fabric publication for the direct
  Windows named-shared-memory route.
- The route is hardware-specific and explicitly marked
  `COMPATIBILITY_FALLBACK`.
- It can coexist with a future generic RGB-D route.
- Orbbec calibration and the optimized direct reader remain provider-local.

## Required before enforcement

1. Connect multiple real Skill execution adapters to the discovered tool
   descriptors and compare agent choices under `required` and `auto`.
2. Make selected capability bindings affect consumer routes, then test binding
   invalidation, provider restart, stale boot IDs, and explicit fallback.
3. Compare Manager advisory authority with every physical provider-local lease
   across normal completion, timeout, preemption, disconnect, and restart.
4. Execute shutdown ordering in a nonphysical process harness, then in a
   guarded hardware environment with explicit recovery checks.
5. Measure audit write latency, disk-full behavior, pending-queue pressure, and
   Fabric replay before considering strict local audit enforcement.
6. Enable one policy at a time, repeat all interconnection tests, and only then
   remove compatibility paths.

## Deliberately deferred

- Extracting and revising `calibrate-stationary-workcell`, including
  FoundationPose-provider retirement.
- General `spatial.registration.rgbd`.
- General multi-model VLM Skill, fallback escalation, QC, and voting.
- `register-tool-to-control-frame`.
- New slicing behavior.
- Full migration of speed, singularity, and route-search policy into
  Integrated. Phase 1 adds the direct controller planning boundary but does not
  rewrite the legacy cutting planner.
- Unified browser-based development and authorization UI.
- Test-agent physical movement to the front or top of a pointed object. This
  remains blocked on enforced authority, per-action authorization, and guarded
  hardware validation.
