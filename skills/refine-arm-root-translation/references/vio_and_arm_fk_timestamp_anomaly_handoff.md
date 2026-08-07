# VIO and Arm-FK Timestamp Anomalies: Investigation Handoff

## Purpose

This note records both VIO and arm-FK timestamp anomalies observed while testing
`refine_arm_root_translation` on 2026-08-06 and 2026-08-07 Pacific time. It
separates one confirmed VIO-context availability failure from the repeated
arm-FK/Fabric transform-history anomaly. They affect the same refinement call
but are different subsystems and should not be diagnosed as one bug.

The source is the local normalized run journal at
`test_agent/run/agent_run_journal.v1.sqlite3`. Times below are UTC.

## Recorded VIO and arm-FK evidence

| Started at | Run ID | Recorded failure | Classification |
| --- | --- | --- | --- |
| 2026-08-07 00:12:17 | `5e6edda2-23eb-4dda-87d0-ff4bd401c42d` | `VIO did not publish a current TRACKING body pose` | VIO status/body-pose availability or consistency |
| 2026-08-07 01:33:01 | `c5bec866-1bc5-488b-bf27-b41eb3db40a7` | Arm FK capture-window end remained extrapolated by 29,383 us | Arm/Fabric transform history |
| 2026-08-07 01:38:27 | `07a69c6d-b2d8-4ef2-b1f5-37e11c3819d0` | Arm FK capture-window end remained extrapolated by 33,914 us | Arm/Fabric transform history |
| 2026-08-07 01:39:33 | `5d4f69ca-c44c-4d1a-806d-316bfb480e89` | Arm FK capture-window end remained extrapolated by 3,184 us | Arm/Fabric transform history |
| 2026-08-07 08:27:45 | `9a25654e-e99d-459d-8e3f-9ed1171385e9` | Initial arm-FK preflight was unavailable; one requested HOT recovery restored bracketing and the fresh retry succeeded | Transient arm/Fabric dependency, recovered |
| 2026-08-07 08:32:20 | `3e0a7605-15c5-48ea-a40f-22d111caefc8` | Initial preflight failed; after one HOT recovery the fresh retry still required about 98 ms of forward extrapolation | Arm/Fabric transform history |
| 2026-08-07 08:34:02 | `9b44402e-e99d-459d-8e3f-9ed1171385e9` | Initial preflight failed; after one HOT recovery the timestamped tool-to-base lookup returned HTTP 404 | Arm/Fabric transform history |
| 2026-08-07 08:34:50 | `8f786d6b-7044-46fd-b89f-7a8b29337dbf` | A refinement-only call with no preceding motion failed before VLM work; after one HOT recovery the fresh retry still required about 408 ms of forward extrapolation | Arm/Fabric transform history |

Earlier runs around 2026-08-06 23:18–23:20 also failed because the arm joint
state was considered too old for timing alignment. That older symptom belongs
with the arm-feedback/FK investigation, not the VIO investigation.

## Repeated-call sequence on 2026-08-07

The later hardware test provides a useful time sequence rather than isolated
failures:

1. Run `c28f9b4f-53e9-4681-b72f-708f7b864efe` started at 08:17 UTC and
   completed a five-sample rail-center refinement. All five landmark reviews
   passed and one translation-only update was applied.
2. Run `41775d98-43ba-406d-b46f-cb9b844bd02d` started at 08:26 UTC and
   completed another refinement normally.
3. Run `9a25654e-e99d-459d-8e3f-9ed1171385e9` started at 08:27 UTC. Its first
   FK preflight failed, but one requested HOT recovery followed by a fresh retry
   succeeded.
4. Beginning with the 08:32 UTC run, the same bounded recovery no longer
   restored timestamp bracketing. The observed residual progressed through
   about 98 ms, a timestamped transform-history 404, and about 408 ms.
5. The final 08:34 UTC run requested only refinement, without a preceding arm
   movement, and still failed before landmark inference.

This sequence does not implicate the VIO path: none of these later failures
reported a missing, stale, or epoch-inconsistent VIO body pose. It also does not
look like rejection by the refinement motion gate or a slow VLM call. The
failure is in the arm-FK dependency preflight, before a usable RGB-D/FK sample
is admitted and before any alignment update is attempted.

The successful recovery at 08:27 proves that the failure can be transient. The
later repeated failures prove that requesting HOT is not sufficient evidence
that the underlying transform publisher or Fabric ingestion resumed. The
journal records the lifecycle request and the retry result, but it does not
record a publisher process restart, a new provider boot identity, or advancing
FK sequence/timestamps. An already-HOT lifecycle request may be a no-op.

The 404 is not evidence that the HTTP route itself is absent, because the same
route succeeds in other calls. In this context it means Fabric could not return
the requested timestamp-bracketed tool-to-base transform from available
history. The increasing forward-extrapolation residual is consistent with a
stalled or intermittently delayed transform stream, but the journal alone
cannot distinguish arm-provider publication stall, Fabric ingestion stall, or
an upstream timestamp/cadence defect.

## What the VIO failure proves

The error included a latest VIO status payload that reported `ready=true`,
`tracking_state=TRACKING`, and `visual_stale_s=0.0`. Nevertheless,
`InitializeSpaceCognitionSkill._wait_for_tracking_context` could not construct a
current tracking context before its timeout.

That method concurrently reads `localization.vio.status` and
`localization.body.pose`. It rejects the pair when either observation is absent,
invalid, or outside its declared freshness; when status is not `TRACKING`; when
the spatial convention differs; or when the status and pose session epoch or
world frame differ. The timeout error prints only the last status payload. It
does not record which predicate failed or the rejected body-pose metadata.

Therefore, the log proves an availability, freshness, or cross-stream
consistency failure. It does not prove that the VIO estimator's pose mathematics
were wrong. The leading hypothesis is that status continued to publish as
healthy while the body-pose observation was missing, stale, delayed, or from a
different epoch. This remains a hypothesis until both raw observations are
captured at the failure boundary.

## Why VIO timing matters to translation refinement

The accepted depth pixel is transformed into the world frame using the camera
pose at the registered-depth timestamp. A stale or epoch-mismatched VIO pose
would move that 3D point to the wrong world location and would appear as a false
arm-root XYZ correction. Failing closed when a current matching pose is not
available is therefore required. Do not bypass the current-pose gate to increase
success rate.

## VIO investigation checklist

1. On every failed `_current_tracking_context` attempt, record both the status
   and body-pose stream metadata: `observed_at_us`, evaluated age,
   `freshness_ms`, `valid`, provider/boot identity, sequence, tracking state,
   session epoch, world frame, and convention ID.
2. Record the exact rejection predicate instead of printing only the last VIO
   status payload.
3. Verify whether VIO status and body pose are published atomically or at least
   with compatible cadence and latest-value semantics. Check for queue backlog
   and a status stream that can advance while the pose stream stalls.
4. Verify that `observed_at_us` and the wall-clock comparison in
   `_observation_is_current` use the same clock domain. A hardware or monotonic
   timestamp must not be compared directly with Unix wall-clock microseconds.
5. Inspect the special case where `freshness_ms` is absent: the current helper
   treats any positive timestamp as current, regardless of age. Decide and
   document whether that is intentional.
6. Confirm recovery behavior by recording the first fresh matching status/pose
   pair after a failure and the duration of the publication gap.

Relevant code is in
`test_agent/python/physical_agent_test/initialize_space_cognition_skill.py`,
especially `_wait_for_tracking_context`, `_current_tracking_context`, and
`_observation_is_current`.

## What the arm-FK failures prove

The recorded arm-FK failures did not originate in VIO. The refinement adapter
copied an RGB-D frame and requested an arm transform at the calculated
capture-window end. Fabric returned a transform path that still required
forward extrapolation or could not return the timestamped path, and no later
history sample arrived within the bounded wait.

The capture-window end is later than the newest RGB/depth timestamp by the
selected arm-feedback age plus the profile's camera timing margin. Even when the
arm is physically stationary, zero-extrapolation bracketing still requires a
timestamped FK sample at or after that window end. If the arm provider publishes
FK only when joint values change, a stopped arm may never provide this closing
sample. A delayed continuous stream or transform-history ingestion backlog can
produce the same symptom.

Do not simply allow the observed 3–408 ms extrapolation. The Skill currently has
no provider attestation that the arm remained constant throughout the missing
interval. Treating an extrapolated pose as exact would weaken the capture-motion
bound.

## Arm/Fabric investigation checklist

1. Record the latest transform-history timestamp, the requested window-end
   timestamp, and the immediately preceding and following joint/FK samples.
2. Verify that the arm provider continues publishing timestamped joint/FK state
   at a documented cadence while the arm is stationary.
3. Measure publication, Fabric ingestion, and query latency separately. Check
   whether latest-value queues are being processed behind older samples.
4. Verify the semantics and clock domain of the arm feedback-age field used to
   extend the capture window.
5. If continuous publication is unavailable, add a provider-level attestation
   for a constant transform interval before considering exact constant-hold
   interpolation. Do not infer stationarity from silence alone.
6. Retest the same between-movement prompt repeatedly and compare the success
   rate with the measured FK publication cadence.
7. At every lifecycle recovery, record the provider lifecycle state before and
   after the request, provider boot ID, process identity, latest raw joint/FK
   sequence, latest Fabric transform timestamp, and whether each value actually
   advanced. Do not equate a successful HOT response with publisher recovery.
8. Compare the raw provider joint/FK timestamp with the latest corresponding
   Fabric edge at the same instant. This separates producer stall from Fabric
   ingestion stall.
9. Record why a timestamped transform query returned 404: missing frame path,
   request newer than history, request older than retained history, identity
   discontinuity, or another cause. A bare 404 loses the evidence needed for
   diagnosis.
10. Run a bounded soak test for at least ten minutes with repeated refinement
    preflights, both with and without intervening motion. Record publication
    cadence, bracketing residual, lifecycle state, and provider/Fabric process
    resource use on every attempt.

Relevant code is in
`skills/refine-arm-root-translation/python/refine_arm_root_translation/host_adapter.py`,
especially `_capture`, `_arm_feedback_age`, and `_bracketed_transform`.

## Scope boundary for the next fix

Keep VIO pose publication and arm-FK transform history as separate work items.
Preserve session-epoch checks, identity checks, bounded waiting, timestamped
interpolation, and zero unqualified extrapolation. The alignment Skill should
consume trustworthy observations; it should not compensate for provider timing
defects with guessed transforms.

The observed failure behavior is safe for alignment state: each unbracketed
attempt failed closed, performed no VLM-based correction, and left the active
translation and rotation unchanged. Treat the arm/Fabric reliability issue as
a known external dependency limitation rather than weakening the refinement
Skill's timestamp requirements.
