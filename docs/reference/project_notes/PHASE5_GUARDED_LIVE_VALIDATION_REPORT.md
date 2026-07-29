# Phase 5 Guarded Live Validation Report

Date: 2026-07-28
Result: guarded hardware checkpoint passed; Phase 5 exit not passed.

Superseding update: the later 2026-07-29 checkpoint used the real OpenAI
Agents SDK to select the narrow decision-ID-only physical execution Skill.
See `PHASE5_AGENT_SDK_COMPLETION_AND_SHUTDOWN_20260729.md`. The report below
continues to describe the earlier guarded non-Agent run and is retained as
historical evidence.

## Scope

This report covers the fresh stationary-workcell calibration, reviewed
activation, controller-owned top-observation transit toward the toilet-paper
roll, no-contact gripper exercise, explicit release, safe-home, and shutdown
fallback. It does not claim that the disabled compound OpenAI Agent SDK tool
selected the sequence.

## Replay and software basis

- The post-checkpoint stopped/nonphysical cross-package local matrix passes
  `578/578` (548 Python and 30 Rust tests). The exact GitHub publication
  candidate passes `469/469` after excluding the local-only cutting prototype.
- Sixteen replay scenarios derive their results from injected evidence and
  cannot start hardware, acquire a physical lease, call a physical controller,
  or enable Agent execution.
- Generic and direct RGB-D replay routes produced an exact `0.0 m` point
  difference on the same immutable observation.
- The stationary-alignment suite passes `68/68` after the rejected-lineage
  fix.
- Integrated passes `95/95` after the final-hold gripper `STOP`, authority
  shadow, and deterministic endpoint-arrival test fixes.
- Basic passes `83/83` with the gripper physical-ceiling translation and
  measured-speed guard.

## Live perception and VLM evidence

- Fresh Orbbec RGB, IR, native depth, and registered depth were captured from
  shared memory with bounded waits.
- Codex built-in multimodal inspection reviewed the exact calibration,
  execution, and open-gripper RGB evidence.
- No external VLM API call was made in this checkpoint.
- Registered depth and RGB used their independent resolutions and the
  provider-authored alignment metadata.

Primary local evidence:

- `skills/stationary_world_arm_alignment/run/20260728T233158Z-3f5c4673/`
- `providers/orbbec_femto_bolt/run/phase5_fresh_live/`
- `providers/orbbec_femto_bolt/run/phase5_gripper_open/`

## Calibration evidence

The first live candidate was explicitly rejected after built-in visual review
showed that its one-sided gripper point was inconsistent with the learned
mean-beak offset. No rejected transform became motion-usable.

The corrected candidate
`20260728T233158Z-3f5c4673` refined the last Manager-verified lineage by
`8.14474 mm`, had confidence `0.82`, received an exact-digest signed review,
and was activated through Manager only after current camera and VIO identity
checks. Its activation was explicitly revoked after the physical test.

## Controller and physical evidence

Plan `e6d2dda6-d820-4700-88bf-f228bcd3f911` was controller-owned,
collision-free, and singularity-checked. The caller supplied only the semantic
scene, target, and speed request. The accepted plan used `0.05 m/s` Cartesian
speed, a `0.25 rad/s` joint ceiling, `0.1530259 m` predicted clearance, and a
minimum Jacobian sigma of `0.0608862`.

The first commit rejected stale scene state without motion. The accepted
one-time signed commit executed forty measured-arrival stages, held the final
endpoint with less than `0.0008 rad` joint error, and required explicit
release. Strict local audit records both accepted and rejected requests.

The gripper opened and closed in free space within the authorized final arm
hold. There was no lease change, float, conflicting writer, duplicate commit,
fault, or object contact. Visual inspection showed symmetric movement and no
obvious damage.

## Safety findings

Measured gripper feedback briefly exceeded the user ceiling despite a lower
target-rate command. The peak observed values were approximately
`0.330 rad/s` opening and `0.256 rad/s` closing. The run therefore stopped
before contact.

Basic now interprets the requested gripper rate as a physical ceiling,
translates it to the hardware-native field with an explicit conservative
scale, and applies a measured-feedback hysteretic brake/hold at the ceiling.
This correction has deterministic trip/hold/resume coverage and telemetry but
has not been physically calibrated or revalidated. Future contact authorization
therefore remains blocked.

The final-hold gripper `STOP` path was corrected after the physical run. It now
removes joint 7 from the next controller-owned envelope so Basic recaptures
the measured gripper angle while the arm endpoint remains held. This change
has software coverage only and was not physically retested.

## Final state and shutdown

- The workcell activation is revoked and no longer motion-usable.
- Integrated released the fenced Basic lease and entered healthy `WARM`.
- Basic safe-home succeeded, preserved the gripper angle, and settled in
  healthy gravity-float before shutdown.
- Manager stopped Integrated, fenced new authority, and correctly refused to
  stop the externally owned Basic process.
- Basic's local authoritative graceful-stop fallback completed.
- Camera, VIO, FoundationPose, Basic, Integrated, and the alignment UI are not
  reachable on their runtime ports.
- Manager and Fabric initially remained running to retain the shutdown and
  audit record. They were subsequently stopped before the final software
  matrix; the current listener audit finds all reviewed core/provider ports
  closed.

## Exit decision

The physical checkpoint passes, but Phase 5 remains active. Enforced explicit
provider fallback and the stopped full regression are now covered in software.
The general effector-front landmark decision was subsequently resolved as a
read-only depth-backed Skill. Remaining blocking work includes its recorded and
live nonphysical VLM review, the real model-selected Agent SDK loop, specialized
action-point semantics, live finite-estimator comparison and FoundationPose
retirement decision, the complete authority lineage/failure matrix and one
reversible non-motion enforcement transition, unified browser UI, physical
gripper-speed calibration/revalidation, and final release-evidence
reconciliation.

## 2026-07-29 addendum

- The narrow Agent SDK execution checkpoint subsequently passed.
- The compound pointed-object Agent Skill remains disabled.
- A final bounded lift, gravity-float, authoritative safe-home, and complete
  service shutdown passed.
- Cartesian direction semantics and cross-frame axis alignment remain an open
  challenge and are not declared solved by the successful observation move.
