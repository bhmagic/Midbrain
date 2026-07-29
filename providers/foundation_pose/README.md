# FoundationPose CAD Object Pose Provider

Version: **0.3.0**

A Midbrain Resource Provider that exposes generic CAD-conditioned RGB-D object
pose estimation and tracking through `perception.object_pose.*` capabilities.
The Midbrain-facing interface is generic; NVLabs FoundationPose is the current
backend adapter.

The Provider is a **measurement reporter**. It publishes camera-relative object
pose observations and timestamped transform measurements to the World State
Fabric. It does not fuse those measurements into canonical robot state and it
does not claim that visually ambiguous geometry has a unique semantic
orientation.

## Midbrain integration

Provider ID:

`perception.object_pose.foundation_pose`

Capabilities:

- `perception.object_pose`
- `perception.object_pose.estimate`
- `perception.object_pose.track`
- `perception.object_pose.bounding_box_init`
- `perception.object_pose.relocalize`
- `perception.object_pose.stop`
- `perception.object_pose.status`
- `perception.object_pose.model_registry`

Consumes:

- `camera.rgbd.bundle`
- `camera.calibration`
- optional `perception.object.mask`

Publishes:

- `perception.object.pose`
- `perception.object_pose.status`
- `transform.foundation_pose.object` using schema `physical_agent.transform`

The Provider uses the source camera acquisition timestamp for pose and transform
observations. Receipt/inference-completion time is not substituted for the
transform timestamp.

The Manager owns Provider lifecycle. The Provider is registered with
`auto_start: false` and is expected to be started/residency-controlled through
the Manager.

### Use from Skills and Agents

Skills and Agents discover this Provider through the Manager capability catalog,
then submit one `track` request per model through the Manager request endpoint.
The default reBot profile provides stable child frames for both reporters:

- `observed_object/rebot_b601_dm/base`
- `observed_object/rebot_b601_dm/gripper_slider_support`

Every successful update publishes a separate `physical_agent.transform` edge
from the configured camera parent frame to the corresponding child frame. The
Fabric indexes transform history by parent/child edge, so the two reporters can
share the `transform.foundation_pose.object` stream without replacing each
other in the transform graph. Consumers can discover the edges with
`GET /v1/transforms` and query either edge with `GET /v1/transform`.

These are raw camera-relative measurements. A camera-alignment Skill may hold
the arm stationary, collect both reporters, estimate an authoritative
world-to-camera calibration edge, and publish that calibration separately.
Once that edge exists, Fabric can compose world-space Base and Gripper poses.
This Provider does not claim or publish that world calibration itself.

## Default robot profile: Seeed reBot B601-DM

The repository ships prepared default geometry for two rigid visual reporters:

| Role | Model ID | Reporting geometry | Stable observed frame |
| --- | --- | --- | --- |
| Robot base | `robot_arm_root` | Base Platform + Base Link | `observed_object/rebot_b601_dm/base` |
| Robot gripper | `robot_gripper_slider_support` | `01_Rail_Bracket` / Gripper Slider Support | `observed_object/rebot_b601_dm/gripper_slider_support` |

The Base pose is published after applying the configured
`mesh_from_semantic` offset so its reporting geometry corresponds to the
configured arm-root frame. The Gripper reporter intentionally uses the centered
rigid Rail-Bracket mesh frame; it is **not** claimed to be the TCP or a URDF
end-effector frame.

Both targets are visually close to symmetric. A valid initialization may return
a reversed orientation hypothesis. This Provider reports the selected visual
measurement instead of forcing an arbitrary semantic direction. A Skill may
combine Base pose, Gripper pose, joint state, or task context to resolve
orientation ambiguity.

Default CAD assets are under `defaults/rebot_b601_dm`. `scripts/setup.ps1` or
`scripts/seed_default_models.ps1` copies them into persistent
`config/foundation_pose` only when files are missing.

## Model assets and training

The default reBot profile contains CAD source, prepared meshes, and frame
metadata. It does **not** contain a reBot-specific fine-tuned FoundationPose
network or a CAD-generated training-image dataset. Model-based FoundationPose
uses the upstream pretrained refiner/scorer at inference time and renders CAD
hypotheses transiently during registration.

The upstream FoundationPose checkout and model weights remain external runtime
dependencies installed under the replaceable Provider directory.

## Runtime data ownership

Robot-specific persistent data belongs outside the Provider:

`config/foundation_pose`

Typical contents:

- `models.json`
- prepared centered meshes
- original-frame meshes
- mesh-preparation metadata
- source CAD/OBJ files
- user masks, captures, and calibration artifacts

The Provider directory is replaceable. A clean Provider reinstall must not
delete `config/foundation_pose`.

## Pose convention

The backend returns:

`camera_from_mesh`

Each model registry entry defines:

`mesh_from_semantic`

The Provider publishes:

`camera_from_semantic = camera_from_mesh @ mesh_from_semantic`

The observed transform edge is deliberately separate from operational robot
kinematics. It must not overwrite a robot-control or fused-state authority.

## Installation

### Midbrain integration only

From Developer PowerShell:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\providers\foundation_pose\scripts\setup.ps1
```

This creates/uses a Provider-local `.venv`, installs the Midbrain integration
package, seeds the default reBot profile when missing, and registers the
Provider.

### Native NVIDIA runtime

The Provider expects a Provider-local NVLabs checkout at:

`providers/foundation_pose/nvlabs/FoundationPose`

The publication release bundle includes a clean native-Windows installer that
pins the tested FoundationPose revision, builds/installs native dependencies,
installs the bundled FoundationPose weights, applies the guarded Windows
temporary-mesh compatibility patch, runs tests, registers the Provider, and
leaves the Midbrain workspace stopped.

Native Windows FoundationPose is an experimentally validated Midbrain path; it
is not presented as an officially supported NVLabs Windows installation mode.

## Requests

Use the Manager request endpoint in normal Midbrain operation. The Provider
accepts the Manager envelope:

```json
{
  "action": "estimate",
  "payload": {
    "model_id": "robot_arm_root",
    "mask_path": "C:\\path\\base_mask.png"
  },
  "request_id": "request-id",
  "related_skill_id": "optional-skill-id"
}
```

Continuous tracking uses `action: "track"` after initialization and retains the
same session state. `track_one()` is the intended low-latency path; repeated
independent full `estimate` requests are substantially more expensive.

Bounding-box initialization uses normalized VLM image coordinates:

```json
{
  "action": "track",
  "payload": {
    "model_id": "robot_arm_root",
    "bounding_box": {
      "box_2d": [250, 100, 750, 600],
      "coordinate_space": "normalized_0_1000",
      "padding_fraction": 0.0
    },
    "max_duration_s": 3600
  }
}
```

`box_2d` is `[ymin, xmin, ymax, xmax]`. Supported coordinate spaces are
`normalized_0_1000`, `normalized_0_1`, and `pixels`. A top-level `box_2d`
array is accepted as shorthand and defaults to `normalized_0_1000`.

Other actions:

- `relocalize`
- `stop`
- `status`
- `list_models`
- `reload_models`

`list_models` returns `role`, `description`, `semantic_frame`, and
`default_child_frame` so Skills can identify the Base and Gripper reporters
without depending on filename conventions.

## Initialization mask input

FoundationPose registration still receives a binary mask. The Provider can
obtain it from the following sources, in precedence order:

- a local `mask_path`;
- a request `bounding_box`, rasterized to a rectangular mask at RGB resolution;
- a latest Fabric observation on `perception.object.mask`.

The mask should contain only visible pixels belonging to the rigid target.
Occluded portions should not be painted in merely to complete the CAD shape.

## VLM + SAM2 tracking GUI

The provider-local GUI keeps orchestration on existing Midbrain interfaces. It
starts Fabric and Manager with the existing workspace script, starts the Orbbec
and FoundationPose Providers through Manager, reads camera and tracking
observations from Fabric, and submits TRACK requests through Manager.

Install the optional, pinned SAM2 runtime once after the normal Provider setup:

```powershell
.\providers\foundation_pose\scripts\setup_sam2.ps1
```

From a Developer PowerShell:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\providers\foundation_pose\scripts\run_tracking_gui.ps1
```

The GUI reads `OPENAI_API_KEY` and the optional `OPENAI_VISION_MODEL` override
from `config/api_keys.env` without displaying its value. Its default is
`gpt-5.6-luna`. The flow is:

1. start Midbrain and both Providers;
2. wait for a live camera image;
3. ask OpenAI Luna for Base and Gripper rectangles plus two high-confidence
   foreground points per object, using bundled CAD atlases;
4. manually replace a box or either pair of points when needed;
5. run SAM2.1 Base+ on each rectangle expanded by 50% total by default, grow
   the Base mask from its median Lab color with distance threshold 30, grow the
   Gripper mask from its median RGB with 10% per-channel tolerance, and apply a
   two-pixel-radius dilation to both masks;
6. inspect the mask overlays and start both mask-initialized TRACK sessions;
7. select a Base rate up to 10 Hz and an experimental Gripper rate up to 60 Hz,
   then inspect live camera-relative pose axes and results. Actual throughput is
   bounded by camera frame rate, GPU latency, and serialized inference.

Prepared FoundationPose estimator geometry is retained in a bounded in-memory
cache while the Provider process remains alive. Cache keys include the mesh
content hash, scale, model revision, and symmetry metadata, so future CAD model
changes cannot silently reuse incompatible prepared data.

VLM output and SAM2 masks are initialization proposals, not safety-certified
detections. The GUI intentionally freezes the review frame and requires visible
review. Keep the robot still until TRACK requests have been submitted, because
FoundationPose registers the reviewed masks against the next camera frame. The
GUI does not command robot motion.

## Custom robot CAD helper

Optional preparation tools are under:

`tools/cad_prepare`

They support the repeatable workflow used for the reBot defaults:

1. isolate one rigid target;
2. export OBJ preserving its original frame;
3. perform minimal cleanup;
4. preserve the cleaned original-frame mesh;
5. create a bounding-box-centered FoundationPose mesh;
6. record the exact centering transform;
7. generate/update a persistent model-registry entry.

See `docs/CAD_PREPARATION.md`.


## Live Manager/Fabric validation

After native setup, a real robot/camera installation can validate the complete
Manager -> Provider -> Fabric path with one controlled initialization:

```powershell
.\providers\foundation_pose\scripts\validate_live_estimate.ps1 `
    -ModelId robot_arm_root `
    -MaskPath C:\path\base_mask.png
```

The validator starts the workspace, starts FoundationPose through the Manager,
sends the request through the Manager request endpoint, requires exactly one
result, verifies both the `perception.object.pose` and
`transform.foundation_pose.object` observations in Fabric, checks that the
source camera timestamp is preserved, then stops the Provider and workspace.

For continuous operation, use `track` after one successful initialization. The
default requested update rate is 3 Hz; actual Base+Gripper throughput is GPU-
dependent because the NVLabs backend serializes inference across sessions.

## Validation status

The publication package includes unit and schema regression tests. The
v0.1.3 runtime baseline was additionally exercised on:

- Windows 11 native
- Python 3.11
- PyTorch 2.7.1 + CUDA 12.8
- NVIDIA GeForce RTX 5070 Ti
- Orbbec Femto Bolt RGB-D
- reBot B601-DM Base and Rail-Bracket reporters

The Base and Gripper each completed real FoundationPose initialization and
produced visually consistent CAD projections. Symmetric orientation ambiguity
is intentionally left for consuming Skills to resolve.

See `VALIDATION.md`.

## Licensing

Midbrain integration code in this Provider is MIT licensed.

NVLabs FoundationPose, its checkpoints, CUDA components, and dependencies are
third-party software and retain their own terms. They are not vendored into the
Git tree by this Provider.

The default reBot B601-DM hardware geometry is derived from Seeed Studio's
`reBot-DevArm` hardware source and remains subject to CERN-OHL-W-2.0. Source
STEP files, the license text, provenance, and modification notices are retained
under `defaults/rebot_b601_dm`.

See `THIRD_PARTY_NOTICES.md`.


### Bundled FoundationPose checkpoints

The complete offline Provider ZIP carries the two model-based checkpoint
sets required by this backend:

- refiner `2023-10-28-18-33-37`
- scorer `2024-01-11-20-02-45`

They are stored under:

`third_party/nvlabs_foundationpose_weights/weights`

The fast updater and clean installer validate their exact byte sizes and SHA-256
digests, install them into the pinned NVLabs checkout, and populate the
persistent machine-local cache:

`config/foundation_pose/install_cache/nvlabs/FoundationPose/weights`

The release installers run checkpoint setup in offline mode. They do not need
Google Drive access.

See `third_party/nvlabs_foundationpose_weights/README.md` and
`WEIGHTS_MANIFEST.sha256` for provenance and hashes.

The scorer checkpoint is about 190 MB and exceeds GitHub's normal 100 MB
single-blob limit. Publishing this exact offline tree to GitHub therefore
requires Git LFS or keeping the weight-bearing ZIP as a release asset. Do not
silently commit the `.pth` files as ordinary Git blobs.
