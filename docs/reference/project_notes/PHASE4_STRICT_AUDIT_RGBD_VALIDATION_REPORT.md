# Phase 4 Strict-Audit and RGB-D Validation Report

Date: 2026-07-27
Result: guarded checkpoint passed; Phase 4 as a whole remains active

## Scope

This checkpoint validated the first independently enforced Phase 4 boundary:
provider-local controller audit. Capability binding, Manager authority, and
generic RGB-D route policy remained in shadow. Agent SDK physical execution
remained disabled. The only authorized physical action was a safe-home followed
by a Cartesian target of `[0, 0, +0.03]` metres. No X/Y translation, rotation,
gripper action, unrelated mode change, or lease experiment was authorized or
performed.

The same checkpoint also validated actual RGB-D image content, not merely route
availability, and verified bounded hard and idle deadlines around VLM,
Skill-adapter, and Agent SDK waits.

## Implemented boundaries

### Independent policy and operation status

`phase4_policy.py` provides independent modes for:

- capability binding;
- controller audit;
- Manager authority;
- generic RGB-D route selection; and
- Agent SDK physical execution.

`GET /api/phase4/policy` reports the live policy state and the bounded-operation
registry. Long external waits report progress heartbeats. Each operation has
both a hard deadline and an idle deadline; a running operation that stops
reporting progress is cancelled instead of being allowed to wait indefinitely.

The validated workspace policy was:

| Boundary | Mode |
| --- | --- |
| Capability binding | `SHADOW` |
| Controller audit | `ENFORCED` |
| Manager authority | `SHADOW` |
| Generic RGB-D route | `SHADOW` |
| Agent SDK physical execution | `DISABLED` |

### Exact controller submissions

Integrated uses `STRICT_LOCAL` with `strict_local_write=true`. Each
state-changing request must first append its exact canonical `SUBMITTED`
record to the provider-local log. Accepted and rejected outcomes are separate
records. Fabric copying remains asynchronous and outside the synchronous
controller path.

The browser development UI exposes a bounded exact-request timeline. The
controller state reports local sequence, published Fabric sequence, pending
count, local failures, and Fabric failures.

The live guarded run ended at local sequence `261` and Fabric cursor `261`.
There were no pending audit records and no local or Fabric publication
failures.

## RGB-D content and timing validation

The Orbbec provider published an atomic route set containing:

- a generic shared-memory descriptor with independent RGB, IR, native-depth,
  and registered-depth geometry;
- custom registration and valid-boundary metadata; and
- the direct Orbbec shared-memory route as an explicit compatibility fallback.

Large RGB and depth payloads remained in shared memory. Fabric carried
BufferRefs, timestamps, channel geometry, alignment metadata, and small
numeric/text state.

The validator read the exact synchronized BufferRefs and checked six
observations. It did not assume equal resolutions, aspect ratios, frame rates,
or image boundaries.

Observed live geometry:

| Channel | Resolution |
| --- | --- |
| RGB | 1920 x 1080 |
| Native depth | 640 x 576 |
| Registered depth | 1920 x 1080 |

The custom valid region was approximately `x=121`, `y=0`, `width=1664`,
`height=1078`. Descriptor-versus-observed boundary IoU was `1.0`; valid depth
covered about `61%` of the registered grid. Maximum RGB/native-depth timestamp
delta was about `1.1 ms`; maximum RGB/registered-depth delta was about
`32.1 ms`, below the configured `50 ms` limit. All channels advanced across
the six-sample window at approximately `30.1 Hz`. The registered RGB/depth edge
agreement score was approximately `0.395`.

Two Gemini VLM calls reviewed generated three-panel RGB, registered-depth, and
overlay composites. Both used the primary model on the first attempt, returned
`PASS` with high confidence, and required no fallback model. The same two
images were also visually inspected in the development session:

- `test_agent/screenshots/rgbd_qc_composite_20260727T225301_064054Z.jpg`
- `test_agent/screenshots/rgbd_qc_composite_20260727T225615_034242Z.jpg`

The second validation was selected through the OpenAI Agents SDK finite-Skill
path. `gpt-5.6-terra` selected `verify_rgbd_image_alignment` through deferred
tool discovery, received a current Manager binding, used the generic route,
and exposed no motion tool. The model-selected run completed in `36.343 s`.
The operation registry returned to `active_count=0`, confirming that the
progress heartbeat and idle tracking did not leave an unmonitored operation.

## Guarded physical validation

### Preflight

Before execution:

- Basic was `HEALTHY` in `SAFE_HOLD_GRAVITY_FLOAT`;
- all seven motor modes were `IMPEDANCE`;
- no Basic mode transition was active;
- Integrated was `HEALTHY`, `HOT`, disengaged, and trajectory-idle;
- Integrated and Basic had the same lease ID and fencing generation;
- controller audit was strict and fully published;
- the measured controlled-frame position was
  `[0.2585339884, -0.0037615073, 0.2127910547] m`;
- the staged target was
  `[0.2585339884, -0.0037615073, 0.2427910547] m`;
- the exact target delta was `[0, 0, +0.03] m`;
- the Integrated preview was collision-free with no physical blockers;
- preview position residual was `0.0000423 m`; and
- minimum Jacobian sigma was `0.051397`.

Safe-home was performed before the motion and preserved the measured installed
gripper angle at `-0.3625926971 rad`.

### Strict failure-path evidence

An initial PowerShell target-construction mistake produced a four-element
position array. Integrated rejected it before motion with
`external target position_m must contain three finite values`. Strict audit
recorded and published both the `SUBMITTED` and `REJECTED` events. No movement
occurred.

This also exercised the corrected rejection path: the original controller
error remained visible while the post-action audit result was kept separate.

### Authorized execution

The corrected three-element target was submitted once. Audit sequences `252`
through `259` cover engage, unarmed teleop, armed one-shot trigger, and trigger
release. Every request has matching `SUBMITTED` and `ACCEPTED` records, an
exact canonical request hash, and confirmed Fabric publication.

The three-second `PRESS_MIT` one-shot moved the controlled frame approximately:

- `+0.314 mm` in X;
- `-0.072 mm` in Y; and
- `+23.765 mm` in Z.

The controller did not falsely claim target arrival. It reported:

- `completion_outcome=DEADLINE_FLOAT_BEFORE_ARRIVAL`;
- `completion_success=false`;
- `target_arrival_confirmed=false`; and
- `float_confirmed=true`.

At the deadline, target residual was approximately `6.24 mm`. This is the
expected observable behavior of the current fixed-duration one-shot: safety
float wins over extending motion beyond the reviewed duration.

After execution, Basic was healthy in gravity float, every joint remained in
impedance mode, the installed gripper angle was unchanged, the lease remained
coherent, and hardware counters reported:

- zero I/O errors;
- zero mode-switch failures; and
- no active mode transition.

A PowerShell formatting expression failed after the motion while calculating a
summary array. The already-completed motion was not repeated. State, completion
outcome, Basic safety, gripper, and audit evidence were recovered from the
controller endpoints and durable audit log.

## Shutdown

The first direct script invocation was blocked by the host PowerShell execution
policy before any endpoint call. The retry changed execution policy only for
that PowerShell process and directly invoked the existing bounded script; it
did not start a nested PowerShell host.

Manager-owned shutdown completed in `7.1 s`. Integrated stop produced strict
audit sequences `260` and `261`, both present in the Fabric cursor. The
shutdown script confirmed the ordered safe path, then stopped the
supervisor-owned core. Final TCP checks found ports `7001`, `7002`, `7101`,
`7102`, `7103`, `8000`, `8791`, and `8793` closed.

## Regression evidence

The Phase 4 software floor is 444 passing tests:

| Component | Passing tests |
| --- | ---: |
| Test Agent | 42 |
| Integrated arm controller | 75 |
| Basic arm provider | 81 |
| Orbbec provider | 10 |
| Local VIO | 30 |
| FoundationPose compatibility package | 43 |
| Manager and Fabric platform core | 18 |
| Vegetable-cutting local prototype | 106 |
| Stationary workcell calibration | 32 |
| Generic spatial registration | 4 |
| Tool-to-control-frame registration | 3 |
| **Total** | **444** |

After the physical shutdown and documentation update, the directly affected
suites were rerun: Test Agent `42/42`, Integrated controller `75/75`, and
Orbbec routing `10/10`.

## Decision

Strict provider-local controller audit is accepted as the first independently
enforced Phase 4 boundary. It is safe to leave enabled in this workspace.

This report does not declare Phase 4 complete. Capability-binding enforcement,
generic-route enforcement for a migrated production consumer, one Manager
authority transition, the complete pointed-object-to-preview Agent loop,
stationary calibration acceptance, FoundationPose retirement, and the final
agent-selected front/top observation move remain separate gates.
