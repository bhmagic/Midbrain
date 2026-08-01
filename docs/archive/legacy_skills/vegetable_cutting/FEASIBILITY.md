# Vegetable Cutting Skill: Version 0.3.0 Feasibility

## Autonomy boundary

This workflow uses bounded agentic perception decisions and a deterministic
motion state machine. The Skill owns perception retries and one bounded
first-cut visual-servo loop after human rejection. Operator
confirmations own tool loading, workpiece loading, execution takeover,
first-cut approval at the physical approach, and tool removal.

The bounded decisions are:

- Retry the initial structured VLM localization up to the configured limit.
- Reject a scene when the board, vegetable, or blade is not confidently visible.
- Reject a scene when a person, hand, or animal is in the robot work area.
- Require valid aligned depth at the two VLM-selected board endpoints.
- Generate cut centers by linear interpolation between those two 3D points.
- Require one quality-passing blade observation with plausible blade
  dimensions, local depth quality, and bounded tool-frame acting-point distance.
- Apply bounded VLM-provided arm-base 6D first-cut offsets in a
  capture-correct-move-recapture loop after takeover and leave final acceptance
  to the human.
- Do not coordinate-revalidate after the human accepts the first-cut approach.
- Split every motion into bounded MIT commits and require an accepted preview
  plus confirmed gravity-float completion before advancing.

## Workflow feasibility

| Requested capability | Version 0.3.0 status | Notes |
| --- | --- | --- |
| Start required Providers | Implemented | Lifecycle-only Manager access is allowed. |
| Use stationary world/arm pose | Implemented prerequisite | Same VIO epoch and camera-calibration revision are required. |
| Prompt for knife loading and grip | Implemented operator gate | The Skill cannot command the gripper. |
| Prompt for vegetable placement | Implemented operator gate | Operator must also confirm leaving the workspace. |
| Find board, vegetable, and blade | Implemented as structured VLM observation | VLM output cannot contain motion fields. |
| Verify endpoint depth | Implemented | Both VLM-selected line endpoints require aligned RGB-D depth. |
| Plan spaced cut centers | Implemented | Centers are linearly interpolated on the straight 3D line between the two endpoint depth points; board shape is ignored. |
| Register blade observation | Session calibration implemented | One observation must pass blade dimensions and a bounded tool-frame acting-point check. Oblique tip-to-heel depth is allowed. If reflective metal returns background depth, the VLM marks the blade/handle junction and a nearby non-reflective handle point toward the gripper; handle depth plus blade image rays and the held-tool axis create a reviewable fallback. |
| Apply blade yaw | Implemented | Yaw is around arm-base positive Z and becomes the calibrated controlled-frame target orientation. |
| MIT transfer | Implemented, physical validation pending | The first transfer uses a 150 mm vertical lift, X/Y transfer at clearance, orientation alignment at clearance, and vertical descent. Every phase uses bounded PRESS_MIT one-shots; requested Cartesian speed supplies a duration floor while provider joint-rate caps remain authoritative. |
| First-cut VLM correction and careful mode | Implemented | The human reviews the physical approach first. `NO_READJUST` invokes a bounded first-cut-only capture-move-recapture loop with at most two default correction moves and a final VLM residual capture; the next human decision is final. |
| MIT cutting stroke | Implemented, physical validation pending | Kp and duration are submitted per stroke. Kp is still a reviewed controller profile multiplier rather than a cut-depth guarantee. |
| Automatic later cuts | Implemented | Later shifts, strokes, and retracts are automatic with no coordinate recheck after human approval. |
| Gripper release prompt | Completion protocol documented | The preview requires operator release through Integrated and proof that the tool is removed. |
| Safe-home after release | Completion protocol documented | The preview orders safe-home only after tool-release proof, followed by Midbrain stop-all verification. |
| GUI stop-to-Float | Implemented | Stop cancels the execution task and requests Integrated gravity-float. |
| Fabric contact objects | Deferred | Matches the requested future scope. |

## Remaining physical validation

- Verify the handle-anchored reflective-blade fallback against the physical
  acting point with the new camera angle.
- Measure payload mass and tool-frame center of mass; the first bring-up uses
  Integrated's documented zero-payload assumption when they are unknown.
- Validate the first approach, full planned cut/retract sequence, tool-removal
  gate, and safe termination on hardware with
  the operator continuously present.
- Tune the MIT Kp multiplier and duration from observed contact behavior.

The next live perception run should begin only after the Integrated controller
has been recovered from any fault, a new stationary alignment records the
camera-calibration revision, the entire cutting board has image margin and
aligned depth, the knife and vegetable are visible, and all people and animals
are outside the robot work area.
