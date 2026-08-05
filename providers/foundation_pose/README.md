# FoundationPose Compatibility Resource Provider

This replaceable Provider exposes the legacy session-oriented FoundationPose
interface for compatibility diagnostics and guarded route comparison. New
bounded workflows should normally use the finite
[`foundation_pose_object_localization` Skill](../../skills/foundation_pose_object_localization/README.md),
which owns estimator resources for one parent operation instead of keeping this
GPU Provider continuously resident.

FoundationPose is a camera-relative measurement reporter. It estimates the
pose of reviewed rigid CAD models and publishes timestamped observations with
camera, calibration, model, session, and quality provenance. It does not
define world space, resolve visual symmetry, activate a workcell calibration,
or grant motion authority.

## Spatial terminology

- Native camera input and output use
  `CAMERA_OPTICAL_X_RIGHT_Y_DOWN_Z_FORWARD_V1`, with components named
  `camera_system_x`, `camera_system_y`, and `camera_system_z`.
- `camera_from_mesh` is the backend result. A registry entry supplies
  `mesh_from_semantic`, and the Provider reports
  `camera_from_semantic = camera_from_mesh @ mesh_from_semantic`.
- `robot/arm_root` is the semantic frame of the default visual Base reporter.
  It is not the controller frame `rebot_arm_base`; only a separately reviewed
  and activated alignment may bind those concepts.
- The default Gripper reporter is the centered rigid Rail-Bracket mesh frame.
  It is not a TCP, URDF end-effector frame, controlled frame, or task action
  point.
- A visually plausible symmetric orientation remains a raw measurement. A
  finite alignment Skill must combine other evidence before using it in a
  motion-relevant transform.

See [Midbrain integration](docs/MIDBRAIN_INTEGRATION.md) for the runtime
boundary and [default reBot profile](docs/REBOT_B601_DM.md) for exact reporter
frames.

## Install

From Developer PowerShell at the repository root:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\providers\foundation_pose\scripts\setup.ps1
```

Setup creates the Provider-local environment, installs the Midbrain adapter,
seeds missing default profile data, and registers the Provider. The native
NVLabs/CUDA path has additional pinned dependencies and compatibility steps;
follow [Installation policy](INSTALL_POLICY.md) and run the gates in
[Validation](VALIDATION.md).

Persistent robot CAD, registry, masks, captures, and calibration artifacts
belong under `config/foundation_pose`. The Provider directory is replaceable;
a clean Provider reinstall must preserve that machine-local configuration.

## Default profile

The supplied Seeed reBot B601-DM profile contains two independent rigid visual
reporters:

| Role | Model ID | Stable observed frame |
|---|---|---|
| Robot base | `robot_arm_root` | `observed_object/rebot_b601_dm/base` |
| Robot gripper bracket | `robot_gripper_slider_support` | `observed_object/rebot_b601_dm/gripper_slider_support` |

The profile contains CAD source, prepared meshes, frame metadata, provenance,
and modification records. It does not contain a reBot-specific fine-tuned
network or assign either reporter operational control authority.

## Diagnostic surfaces

The Manager-hosted `/dev` surface is the preferred model-generic Provider UI.
It exposes backend residency, the model registry, sessions, raw
camera-relative measurements, direct diagnostic requests, and explicit
resource release.

A legacy VLM + SAM2 tracking GUI remains for compatibility and hardware
diagnosis:

```powershell
.\providers\foundation_pose\scripts\setup_sam2.ps1
.\providers\foundation_pose\scripts\run_tracking_gui.ps1
```

That GUI crosses the desired component boundary by orchestrating workspace
lifecycle, robot-specific Base/Gripper selection, VLM proposals, mask review,
and multi-object tracking. Its proposals are not safety-certified, it requires
human review of a frozen frame, and it never commands robot motion. New
Provider integrations must not copy that orchestration into the generic
measurement contract.

## Documentation

Human and installation-agent entry points:

- [Installation policy](INSTALL_POLICY.md) — fast replacement, clean reinstall,
  persistent data, and offline checkpoint cache.
- [Validation](VALIDATION.md) — publication, native backend, Manager/Fabric,
  and remaining deployment checks.
- [Default reBot profile](docs/REBOT_B601_DM.md) — exact reporter roles,
  geometry, transforms, symmetry, and observed frames.
- [CAD preparation helper](tools/cad_prepare/README.md) — prepare another
  rigid target without making the Provider robot-specific.

Coder and coding-agent references:

- [Midbrain integration](docs/MIDBRAIN_INTEGRATION.md) — Provider-versus-Skill
  boundary, request actions, lifecycle, frames, and BufferRefs.
- [`manifest.json`](manifest.json) — authoritative version, capabilities,
  streams, route policy, and readiness metadata.

History and compliance:

- [Changelog](CHANGELOG.md) — release history; not current operating guidance.
- [Third-party notices](THIRD_PARTY_NOTICES.md) — NVLabs, SAM2, and Seeed
  licensing boundaries.
- [NVLabs checkpoint provenance](third_party/nvlabs_foundationpose_weights/README.md)
  and [Windows compatibility source](third_party/nvlabs_windows_compat/README.md).
- [reBot CAD provenance](defaults/rebot_b601_dm/UPSTREAM.md) and
  [modifications](defaults/rebot_b601_dm/MODIFICATIONS.md).
