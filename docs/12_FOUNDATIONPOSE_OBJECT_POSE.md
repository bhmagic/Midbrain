# FoundationPose Base and Gripper Object Pose

## Purpose and authority boundary

The FoundationPose Provider supplies CAD-based 6D pose measurements for two robot-arm targets and publishes them through standard Midbrain observations and Fabric transform edges. It is a persistent perception service: other Skills and Agents can discover and consume its output without depending on its tracking GUI.

The Provider owns camera-relative object measurements. It does not own robot motion, camera-to-world calibration, or world-frame truth. A separate bounded Skill must establish any world alignment and publish it under its own authority.

## Published targets

| Target | Provider target ID | Fabric child frame |
|---|---|---|
| Robot Base | `robot_arm_root` | `observed_object/rebot_b601_dm/base` |
| Gripper root/slider support | `robot_gripper_slider_support` | `observed_object/rebot_b601_dm/gripper_slider_support` |

Each target uses its prepared CAD geometry and cached FoundationPose assets. The cache identity incorporates source content and preparation settings so future CAD models can be added without reusing incompatible derived data.

## Capabilities and outputs

The Provider registers with the Manager and exposes object-pose initialization, tracking, status, and target discovery through its manifest and control API. Its principal Fabric outputs are:

- `perception.object.pose` observations carrying target identity, camera-relative pose, timestamps, validity, and diagnostics.
- `transform.foundation_pose.object` observations backing dynamic transform edges from the selected camera optical frame to each observed target frame.
- Provider and per-target status used to distinguish initialization, tracking, loss, errors, and requested versus achieved tracking rate.

The two targets share an observation stream schema, but the Fabric stores transform history by parent/child edge. Consumers should select by frame identity, not merely by the latest stream item.

## Installation

Set up the core Midbrain workspace first. Then, from the repository root:

```powershell
git lfs pull
.\providers\foundation_pose\scripts\setup.ps1
.\providers\foundation_pose\scripts\setup_sam2.ps1
```

The first script creates the Provider environment, installs integration dependencies, seeds missing local configuration, and registers the Provider at its default control port. The SAM2 script installs the optional local segmentation path.

These scripts do not build the complete upstream NVLabs FoundationPose CUDA runtime. Prepare that compatible runtime separately and keep its checkout, compiled extensions, and environment outside Git.

## Tracking GUI workflow

Launch the core workspace and RGB-D camera Provider, then run:

```powershell
.\providers\foundation_pose\scripts\run_tracking_gui.ps1
```

Use this initialization sequence:

1. Physically secure or separately disable the arm and keep it still.
2. Freeze a synchronized RGB-D frame with both rigid targets visible.
3. Ask the OpenAI visual localizer, which defaults to `gpt-5.6-luna`, for a bounding box and two positive points that lie unambiguously on each object.
4. Review the proposed regions manually. Correct or reject boxes and points that include the wrong link, background, cable, or floor.
5. Run SAM2 on only the target box plus 50% padding. Cropping reduces segmentation work and limits distractors.
6. Refine and dilate the mask. The tested Base default is median Lab distance 30 plus radius-2 dilation. The tested neon-green Gripper-root default is a median RGB seed with 10% per-channel drift plus radius-2 dilation.
7. Inspect the final mask. Prefer a connected mask covering rigid CAD-matched surfaces; do not start from a fragmented or contaminated result.
8. Submit initialization, wait for registration to finish, and confirm both target transforms appear in the Fabric before releasing the arm.

OpenAI and SAM2 are initialization aids only. Manual regions can replace them, and Fabric consumers do not need either dependency after tracking begins.

## Tracking rate

Base tracking is selectable up to 10 Hz. The experimental Gripper selector exposes rates up to 60 Hz to test GPU headroom and measurement stability. The requested rate is a ceiling: actual throughput is limited by RGB-D arrival, serialized GPU inference, registration/tracking cost, and hardware load.

Higher Gripper rate did not resolve the observed pose instability. Treat mask quality, CAD symmetry, depth quality, occlusion, and target geometry as the primary debugging variables; use rate control mainly to balance latency and GPU load.

## Consuming transforms from a Skill or Agent

Discover the Provider through the Manager capability catalog and discover current transform edges through the Fabric. To inspect the graph manually:

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

A future alignment Skill should:

1. Acquire motion inhibit or otherwise verify that the arm and camera are stationary.
2. Collect a time window of Base and Gripper measurements with synchronized timestamps and quality metadata.
3. Reject warm-up, lost-track, outlier, and stale samples.
4. Aggregate translation and rotation robustly rather than accepting one frame.
5. Resolve symmetric CAD solutions using known Base/Gripper geometry, robot joint state, or another independent constraint.
6. Solve the camera-to-world transform and estimate uncertainty.
7. Publish that alignment under the Skill's own source identity and coordinate-frame authority.
8. Allow the Fabric transform graph to compose world, camera, Base, and Gripper relationships.

This separation prevents a perception Provider from silently redefining world space and makes the alignment procedure reproducible by another Agent.

## Validation and limitations

Run the Provider suite from the repository root:

```powershell
.\providers\foundation_pose\scripts\validate_publication.ps1
```

The v0.3.0 publication passed 43 regression tests and a live Manager/Fabric integration validator. This confirms packaging and framework behavior; it is not metrology qualification or safety certification. External 6D ground truth, symmetry trials, mask perturbation tests, occlusion/reacquisition trials, long-duration tracking, and camera-alignment accuracy remain to be measured.

## Licensing

The two published FoundationPose checkpoint files and the NVLabs integration are governed by `providers/foundation_pose/third_party/nvlabs_foundationpose_weights/NVIDIA_SOURCE_CODE_LICENSE.txt`, which limits use to non-commercial research and evaluation. The reBot B601/ER1.6 CAD-derived assets retain their CERN-OHL-W-2.0 license at `providers/foundation_pose/defaults/rebot_b601_dm/licenses/CERN-OHL-W-2.0.txt`, with attribution beside the prepared profile. Neither set of third-party materials is relicensed under Midbrain's MIT License.
