# Phase 5 Agent SDK Completion and Final Shutdown

Date: 2026-07-29
Result: narrow Agent SDK physical checkpoint passed; Phase 5 remains active

## Completed result

The real OpenAI Agents SDK selected the finite
`execute_reviewed_observation_motion` Skill. This was not a simulated agent
selection and was not a direct replacement by the coding assistant.

The model received only an already approved decision ID. It did not receive or
select Cartesian coordinates, joint targets, speed, contact permission,
controller mode, lease operations, safe-home, shutdown, or fallback motion.
The host revalidated the immutable authorization record and restaged the exact
reviewed semantic scene before issuing the one-time assertion.

- Decision: `6fb800ec-5b4b-4242-80ee-f0744c7f6cc8`
- Plan: `a47557e6-ea33-4862-a3b9-b6790baee963`
- Assertion: `6f6cd56c-b6e8-4908-ab93-95c64eb6a0ee`
- Scene: `toilet-roll-20260729T0815Z-agent-sdk`
- Selected route: direct and collision-free
- Stages: 40/40 completed
- Maximum joint speed: 0.25 rad/s
- Effective Cartesian speed: 0.05 m/s
- Modeled minimum clearance: 0.07943 m
- Final controlled-frame error: 0.001185 m
- Measured vertical standoff: 0.099942 m
- Contact: none
- Accepted authorized transits: one
- Rejected authorized transits: zero

Fresh post-motion RGB-D evidence showed visible separation from the toilet
roll. Metric registration and measured kinematics remain the stronger
no-contact evidence.

## Post-completion safety behavior

Integrated initially retained the reviewed endpoint as designed. A later
platform/authority-loss event released the retained transit, latched
Integrated `DEGRADED`, and requested the configured gravity-supported error
fallback. Basic remained connected, healthy, and under its renewing local
lease.

The later operator-authorized shutdown sequence began from confirmed
gravity-float:

1. A requested 20 cm upward lift was reduced by normal controller safety
   gates.
2. Targets of 14 cm and greater were rejected by workspace, singularity, IK,
   joint-jump, or joint-travel policy.
3. A collision-free 13 cm preview passed.
4. The measured displacement was approximately 12.67 cm.
5. The eight-second one-shot ended as
   `DEADLINE_FLOAT_BEFORE_ARRIVAL` with approximately 7 mm Cartesian residual
   and confirmed gravity-float.
6. Authoritative safe-home completed and preserved the existing gripper
   policy.
7. Integrated, Basic, camera, Local VIO, FoundationPose, Manager, Fabric, and
   the standalone Test Agent stopped.
8. Final process, listener, and endpoint checks found no remaining Midbrain
   workspace service.

## Completed systematic changes

- Passive timestamped Fabric with Skill-owned freshness decisions.
- Generic shared-memory RGB-D description with provider-specific direct
  fallback.
- Local VIO provider-local latest-reference recovery from recycled Fabric
  BufferRefs.
- Finite stationary workcell calibration with reviewed Manager activation.
- General VLM routing and RGB-D content/alignment validation.
- Read-only RGB-D point registration.
- General effector-front landmark registration.
- Review-only tool-to-control-frame candidate construction.
- Controller-owned general transit planning and speed/singularity/collision
  policy.
- Direct synchronous controller audit with asynchronous Fabric copy.
- Decision-specific browser/operator authorization.
- Decision-ID-only Agent SDK physical execution.
- Error-to-gravity-float, safe-home, and full shutdown validation.
- Browser development GUIs using the shared neutral dark palette.
- Local-only, nondiscoverable preservation of the vegetable-cutting prototype.

## Remaining limitations

- Cartesian-axis interpretation and calibration alignment remain open; see
  `CARTESIAN_AXIS_ALIGNMENT_OPEN_ISSUE_20260729.md`.
- The compound pointed-object front/top Skill remains nondiscoverable until its
  structured pointing/object landmark contract is complete.
- Authorization and one-time assertion state remain process-local.
- Calibration review and activation retry identity need one durable
  transaction.
- A cross-stage expiry-budget orchestrator is still required.
- Manager authority is not yet the sole enforced physical writer authority.
- FoundationPose remains a slow compatibility route owned by the finite
  calibration Skill.
- A generic non-Orbbec camera producer has not yet validated the direct-route
  fallback contract.
- Slice cutting, task-specific action points, VLM voting, grasping, and
  contact-rich behavior remain explicitly deferred.

## Publication validation

- Prepared the GitHub update from an isolated clean clone of
  `bhmagic/Midbrain`.
- Excluded active API-key/system/provider configuration, device calibration,
  captures, logs, shared-memory/runtime state, build products, hard-copy
  baselines, and ignored upstream FoundationPose/SAM2 checkouts.
- Preserved the two already-published FoundationPose model checkpoints through
  Git LFS.
- Passed the clean-configuration audit, 183-file publication Python
  compilation, 52 publication JSON parses, and 80 publication PowerShell
  parser checks.
- Passed all 548 Python tests across the 78-file full local test matrix,
  including 109 vegetable-cutting tests that remain local.
- After excluding the local-only cutting prototype, passed all 439 Python
  tests across every one of the 62 GitHub-candidate test files.
- Passed all 30 Rust tests, Rust formatting, and the Rust release build.
- Corrected the Orbbec test entrypoint import so Local VIO's generic
  `provider.py` cannot shadow it in combined CI.
- Corrected CI dependencies/source roots and enabled isolated pytest imports
  for same-named component test modules.
- The combined stopped local floor is `578/578`; the exact GitHub publication
  floor is `469/469`.

## Phase decision

The narrow physical Agent SDK checkpoint is complete. Phase 5 itself remains
active because the broader compound agent loop, durable authorization,
authority enforcement, axis validation, compatibility retirement, and full
release regression criteria are not all complete.
