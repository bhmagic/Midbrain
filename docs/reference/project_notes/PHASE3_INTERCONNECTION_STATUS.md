# Phase 3 Interconnection Status

Date: 2026-07-27
Status: software integration active; Manager shutdown enabled after guarded
validation; other policy boundaries remain advisory or progressively enforced

## Current policy state

| Boundary | Implemented path | Current enforcement | Remaining proof |
| --- | --- | --- | --- |
| Manager shutdown | Asynchronous provider sequence with fence, acknowledgements, safe-state request, failures, and supervisor handoff | Enabled in this validated workspace; disabled in install template | Passed after Basic classification fix |
| Capability binding | Deterministic binding plus live instance/boot/readiness revalidation | Advisory | Observe stale/fallback behavior before enforcement |
| Direct controller audit | Exact local pre-action copy and asynchronous Fabric replay | `STRICT_LOCAL` in this workspace; template remains shadow | Passed failure-path and guarded `+0.03 m Z` validation |
| Controller planning | Singularity, speed, continuity, collision, residual, and completion outcome owned by Integrated | Planning rejection active; legacy callers retained | Migrate one caller at a time |
| RGB-D transport | Atomic generic-plus-direct route set over shared memory | Generic preferred by compatible consumers | Migrate each remaining consumer |
| Finite perception Skills | Stationary calibration, VLM analysis, spatial registration, and tool-frame candidate packages | Discoverable; adapters remain allowlisted | Guarded FoundationPose route comparison and adapter wiring |
| Authorization UI | Decision-specific browser popup | Records decisions only | Authentication and later execution boundary |
| Replacement Test Agent | Nonphysical front/top proposal plus real Integrated shadow preview | No execution tool offered | Connect the full selected-Skill pipeline before motion |

## Closed interconnection mismatches

- Fabric latest-value semantics previously let the second RGB-D route
  publication hide the first. Orbbec now publishes one route-set observation
  containing both the generic and direct routes.
- Spatial and tool-registration manifests previously required the generic
  route, contradicting the direct fallback. Route capabilities are now
  optional alternatives with an explicit execution-time preference policy.
- The observation-motion API previously created authorization from a locally
  constructed controller request without calling Integrated. It now requires a
  real, bounded, nonphysical Integrated preview and does not open authorization
  when the preview is rejected or unavailable.
- Fixed-duration PRESS_MIT completion previously exposed arrival fields but
  lacked a single caller-facing success outcome. It now distinguishes
  deadline-before-arrival from stable arrival without changing the float
  deadline.
- An audit failure after a target operation could obscure the controlled
  outcome. Post-action audit failure is now reported separately.

## Known compatibility seams

- The Manager's binding language is conjunctive. Alternative RGB-D routes are
  therefore selected from the provider's route-set after the camera provider
  is bound, rather than expressed as two mandatory capabilities.
- External Skill entrypoints are descriptors, not permission to run arbitrary
  code. A Skill becomes an agent tool only after an explicit adapter is
  registered and allowlisted.
- FoundationPose provider compatibility remains the default stationary
  calibration route until the finite Skill-local backend passes guarded
  comparison.
- Vegetable cutting retains its legacy interpolation only for shadow
  comparison. It is not the owner of general path quality and is not
  discoverable for normal agent use.
- The legacy FoundationPose development window is not browser-based. It remains
  a compatibility UI until the provider retirement decision.
- Manager shutdown execution state is intentionally in memory. If Manager
  disappears during the sequence, the supervisor must use the local
  authoritative arm safety fallback; it must not assume completion.

## Completed hardware gate

The Manager-shutdown validation completed with this guarded sequence:

1. independently start and health-check core processes with hard deadlines;
2. start Basic and confirm lease-free gravity float;
3. safe-home while preserving the installed gripper state;
4. start Integrated and stage no more than `+0.03 m Z`;
5. execute the one authorized raise and verify its completion outcome;
6. invoke the enabled Manager provider shutdown sequence;
7. verify Integrated relinquishment, Basic safe-home acknowledgement, Basic
   stop only after that acknowledgement, and supervisor-owned Fabric/Manager
   stop;
8. if any acknowledgement is missing, retain Basic/core when possible and use
   the local authoritative fallback.

No other arm translation, rotation, gripper command, controller-mode
experiment, or lease-handover experiment was included. The full evidence and
the safety-critical classification defect caught before execution are recorded
in `PHASE3_GATE1_MANAGER_SHUTDOWN_VALIDATION_REPORT.md`.

## Current software regression

The bounded nonphysical matrix passes 444 tests:

| Component | Passing tests |
| --- | ---: |
| Integrated arm controller | 75 |
| Basic arm provider | 81 |
| Orbbec provider | 10 |
| Local VIO | 30 |
| FoundationPose compatibility package | 43 |
| Manager and Fabric platform core | 18 |
| Test Agent, discovery, routing, authorization, and UI | 42 |
| Vegetable-cutting local prototype | 106 |
| Stationary workcell calibration | 32 |
| Generic spatial registration | 4 |
| Tool-to-control-frame registration | 3 |
| **Total** | **444** |
