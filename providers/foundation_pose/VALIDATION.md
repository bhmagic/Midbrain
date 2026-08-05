# Validation

## Publication package validation

The Provider publication tree must pass all of the following before merge:

1. Python source compilation.
2. Provider regression tests.
3. JSON/schema parse validation.
4. Default reBot registry load with both meshes present.
5. Package hygiene checks:
   - no `.venv`;
   - no `nvlabs` checkout;
   - no model weights;
   - no `.git`;
   - no captures;
   - no backups;
   - no runtime logs/debug output;
   - no Python caches.
6. File-integrity manifest generation.


## Repository/static validation command

After Provider setup:

```powershell
.\providers\foundation_pose\scripts\validate_publication.ps1
```

This compiles Provider/helper Python, runs regression tests, parses JSON files,
checks the default reBot profile and its integrity manifest, and rejects runtime
artifacts that must not be committed.

## Live Manager/Fabric validation command

Use a mask captured for the current physical scene:

```powershell
.\providers\foundation_pose\scripts\validate_live_estimate.ps1 `
    -ModelId robot_arm_root `
    -MaskPath C:\path\base_mask.png
```

Repeat with `robot_gripper_slider_support` and its visible-object mask when
validating the Gripper reporter. The script owns the workspace lifecycle and
leaves Manager, Fabric, camera, and FoundationPose processes stopped even after
a validation failure.

## Real hardware validation baseline

The retained native runtime baseline was exercised on Windows with:

- Python 3.11.9;
- PyTorch 2.7.1 + CUDA 12.8;
- NVIDIA GeForce RTX 5070 Ti;
- Orbbec Femto Bolt RGB-D;
- Seeed reBot Arm B601-DM.

Verified runtime path:

1. Manager starts the Provider.
2. Provider registers and reports health/readiness.
3. Provider reads `camera.rgbd.bundle` through generation-checked named
   shared-memory BufferRefs.
4. Provider uses aligned metric depth plus RGB intrinsics.
5. Base initialization completes.
6. Gripper Rail-Bracket initialization completes despite central occlusion.
7. Pose observations and `physical_agent.transform` measurements are published
   to Fabric with the source camera acquisition timestamp.
8. CAD projections are visually consistent with the observed rigid targets.
9. Workspace cleanup stops Provider/Manager/Fabric processes after tests.

## Base behavior

Default model:

`robot_arm_root`

The Base geometry is nearly 180-degree yaw symmetric. Independent
initializations showed two approximately reversed orientation families while
metric position remained useful. This is accepted as a visual-measurement
property rather than hidden by the Provider.

Consumers that require unique yaw should combine other information such as
Gripper observation, joint state, or task context.

## Gripper behavior

Default model:

`robot_gripper_slider_support`

The rigid Rail-Bracket is often partially occluded. A tested initialization used
only visible Rail-Bracket pixels and completed successfully. The resulting
full-CAD surface projection was physically consistent with the live RGB-D
observation.

The default Gripper reporting frame is the centered rigid Rail-Bracket mesh
frame, not a claimed TCP frame.

## Tracking validation still required for deployment tuning

Before relying on a particular update frequency, benchmark:

- one Base TRACK session;
- one Gripper TRACK session;
- simultaneous Base + Gripper sessions;
- achieved `track_one()` latency and Hz;
- acquisition-to-publication age;
- steady-state RAM/VRAM;
- relocalization after tracking loss.

The Provider serializes NVLabs backend inference so simultaneous sessions
time-share the GPU.

## Native Windows backend gate

Run:

```powershell
.\providers\foundation_pose\scripts\check_backend.ps1
```

A pass requires CUDA-enabled PyTorch, nvdiffrast, the pinned FoundationPose
runtime, scorer/refiner models, and a working CUDA rasterization context.

## Release-version consistency

Publication validation requires `VERSION`, `manifest.json`, `provider.py` (`PROVIDER_VERSION`), and `python/pyproject.toml` to report the same version.


## Weight-installation validation

The publication regression suite validates Drive-entry selection, minimum file
validation, and release-version consistency without downloading third-party
weights. Real-machine installation must additionally verify both required
checkpoint directories and run the native CUDA smoke test.

`WAITING_FOR_INPUTS` while the camera publishes its first calibration/RGB-D
bundle is a normal transient session state and is not a failed FoundationPose
estimate.



## Bundled checkpoint integrity

Publication validation verifies the four bundled checkpoint files by exact byte
size and SHA-256 before the release is accepted. Runtime installation performs
the same validation before copying weights into the NVLabs checkout or
persistent cache.

Release installers use the bundled payload in offline mode; Google Drive
availability is not part of the installation gate.
