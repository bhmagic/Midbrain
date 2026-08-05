# FoundationPose Base and Gripper Object Pose

## Purpose and authority boundary

The `foundation_pose_object_localization` Skill supplies CAD-based 6D pose
measurements for known rigid targets inside bounded parent workflows. It is
finite because model loading and registration are expensive and most robot
tasks do not need continuous FoundationPose inference.

The Skill owns camera-relative object measurements and its estimator-resource
lifetime. It does not own robot motion, camera-to-world calibration, or
world-frame truth. A parent such as Stationary Alignment must establish any
world alignment and publish it under its own authority.

The former `perception.object_pose.foundation_pose` Provider remains available
as a compatibility route. Neither it nor the finite Skill is a default
Stationary Alignment route, and neither is selected as an automatic fallback.
The regular Agent summons the finite initializer only for the exact operator
request `Use FoundationPose to establish the stationary world-to-arm-base
transform.`

## Published targets

| Target | Provider target ID | Fabric child frame |
|---|---|---|
| Robot Base | `robot_arm_root` | `observed_object/rebot_b601_dm/base` |
| Gripper root/slider support | `robot_gripper_slider_support` | `observed_object/rebot_b601_dm/gripper_slider_support` |

Each target uses its prepared CAD geometry and cached FoundationPose assets. The cache identity incorporates source content and preparation settings so future CAD models can be added without reusing incompatible derived data.

## Skill contract and outputs

The finite Skill requires synchronized RGB-D evidence, camera intrinsics, a
known model ID, and an explicit reviewed mask or future typed bounding-box
reference. During one invocation it can register, track, and collect multiple
samples for its parent. It publishes lifecycle status on:

- `skills.foundation_pose_object_localization.status`

The parent receives `CAMERA_FROM_SEMANTIC_OBJECT` samples directly and remains
responsible for epoch validation, aggregation, ambiguity resolution, and
authoritative publication. The compatibility Provider continues publishing
`perception.object.pose` and `transform.foundation_pose.object` for legacy
consumers and guarded comparison.

## Installation

Set up the core Midbrain workspace first. Then, from the repository root:

```powershell
git lfs pull
.\providers\foundation_pose\scripts\setup.ps1
.\providers\foundation_pose\scripts\setup_sam2.ps1
```

Then install the finite runtime into Stationary Alignment:

```powershell
.\skills\stationary_world_arm_alignment\scripts\setup.ps1
```

The Provider setup preserves compatibility dependencies, seeds missing local
model configuration, and registers the optional compatibility process. The
Stationary setup installs the finite Skill runtime into the environment that
owns the bounded job. SAM2 remains an optional legacy mask-development path.

These scripts do not build the complete upstream NVLabs FoundationPose CUDA runtime. Prepare that compatible runtime separately and keep its checkout, compiled extensions, and environment outside Git.

## Compatibility tracking GUI workflow

Launch the core workspace and RGB-D camera Provider, then run:

```powershell
.\providers\foundation_pose\scripts\run_tracking_gui.ps1
```

Use this legacy initialization sequence only for diagnostics or guarded route
comparison:

1. Physically secure or separately disable the arm and keep it still.
2. Freeze a synchronized RGB-D frame with both rigid targets visible.
3. Ask the OpenAI visual localizer, which defaults to `gpt-5.6-luna`, for a bounding box and two positive points that lie unambiguously on each object.
4. Review the proposed regions manually. Correct or reject boxes and points that include the wrong link, background, cable, or floor.
5. Run SAM2 on only the target box plus 50% padding. Cropping reduces segmentation work and limits distractors.
6. Refine and dilate the mask. The tested Base default is median Lab distance 30 plus radius-2 dilation. The tested neon-green Gripper-root default is a median RGB seed with 10% per-channel drift plus radius-2 dilation.
7. Inspect the final mask. Prefer a connected mask covering rigid CAD-matched surfaces; do not start from a fragmented or contaminated result.
8. Submit initialization, wait for registration to finish, and confirm both target transforms appear in the Fabric before releasing the arm.

OpenAI and SAM2 are initialization aids only. Normal Stationary Alignment
creates and reviews its own bounded evidence and does not require a resident
tracking process.

## Tracking rate

Base tracking is selectable up to 10 Hz. The experimental Gripper selector exposes rates up to 60 Hz to test GPU headroom and measurement stability. The requested rate is a ceiling: actual throughput is limited by RGB-D arrival, serialized GPU inference, registration/tracking cost, and hardware load.

Higher Gripper rate did not resolve the observed pose instability. Treat mask quality, CAD symmetry, depth quality, occlusion, and target geometry as the primary debugging variables; use rate control mainly to balance latency and GPU load.

## Consuming finite results from a Skill or Agent

Discover and invoke the parent finite Skill rather than waiting for a
continuously updated FoundationPose transform. Stationary Alignment binds each
result to synchronized capture provenance and a VIO session epoch, validates
the candidate, and publishes reviewed world-to-arm calibration under its own
authority.

For compatibility diagnostics, current Provider transform edges can still be
inspected manually:

```powershell
Invoke-RestMethod http://127.0.0.1:7002/v1/transforms
```

To request the Base-to-camera mapping, URL-encode the frame values when building a general client:

```powershell
$cameraFrame = 'femto_bolt_color_optical_frame'
$baseFrame = 'observed_object/rebot_b601_dm/base'
Invoke-RestMethod "http://127.0.0.1:7002/v1/transform?from_frame=$baseFrame&to_frame=$cameraFrame"
```

Use the Gripper child frame for the second transform. Check timestamps, validity, target status, and acceptable age before using either measurement. Holding the arm stationary for the requested capture interval does not by itself make stale or ambiguous results valid.

## Camera-to-world alignment workflow

The bounded `skills/stationary_world_arm_alignment` Skill implements this
boundary. It:

1. Acquires motion inhibit and verifies that the arm and camera are stationary.
2. After the exact invocation above, invokes the finite FoundationPose Skill
   for the selected mode and collects a
   time window of Base/Gripper measurements with synchronized timestamps and
   quality metadata.
3. Rejects warm-up, lost-track, outlier, and stale samples.
4. Aggregates translation and rotation robustly rather than accepting one frame.
5. Resolves symmetric CAD solutions using known Base/Gripper geometry, robot joint state, or another independent constraint.
6. Solves the camera-to-world and arm-base transforms and retains source diagnostics.
7. Publishes that alignment under the Skill's own source identity and coordinate-frame authority.
8. Verifies that the nested Skill released every owned session and backend
   resource before the parent result succeeds.

This separation prevents a perception Provider from silently redefining world space and makes the alignment procedure reproducible by another Agent.

The Skill exposes `foundation_base_gripper`, `foundation_base_vlm_gripper`, and `vlm_gripper_only`. Result schema version 2 labels FoundationPose gripper evidence as the gripper model origin and VLM RGB-D evidence as the foremost-beak mean, so downstream consumers do not treat the raw positions as the same physical point.

## Validation and limitations

Run the finite Skill, Stationary Alignment, and compatibility Provider suites
from the repository root:

```powershell
.\skills\stationary_world_arm_alignment\.venv\Scripts\python.exe -m pytest -q skills\foundation_pose_object_localization\python\tests
.\skills\stationary_world_arm_alignment\.venv\Scripts\python.exe -m pytest -q skills\stationary_world_arm_alignment\python\tests
.\providers\foundation_pose\scripts\validate_publication.ps1
```

The v0.3.0 publication passed 43 regression tests and a live Manager/Fabric integration validator. This confirms packaging and framework behavior; it is not metrology qualification or safety certification. External 6D ground truth, symmetry trials, mask perturbation tests, occlusion/reacquisition trials, long-duration tracking, and camera-alignment accuracy remain to be measured.

## Licensing

The two published FoundationPose checkpoint files and the NVLabs integration are governed by `providers/foundation_pose/third_party/nvlabs_foundationpose_weights/NVIDIA_SOURCE_CODE_LICENSE.txt`, which limits use to non-commercial research and evaluation. The reBot B601/ER1.6 CAD-derived assets retain their CERN-OHL-W-2.0 license at `providers/foundation_pose/defaults/rebot_b601_dm/licenses/CERN-OHL-W-2.0.txt`, with attribution beside the prepared profile. Neither set of third-party materials is relicensed under Midbrain's MIT License.
