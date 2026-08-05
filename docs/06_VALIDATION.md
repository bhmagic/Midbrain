# Validation

## 2026-07-29 guarded Phase 5 validation

The current checkpoint includes offline tests plus operator-observed physical validation. The physical run used a real OpenAI Agents SDK model only for Skill selection. Authorization, decision validity, lease ownership, trajectory preview, execution, and audit remained deterministic server-side.

This section is a historical acceptance record. Its 0.25 rad/s, 0.05 m/s,
and small-step limits do not describe the current ordinary-motion policy.

- The Agents SDK selected `execute_reviewed_observation_motion` using only a reviewed decision ID.
- The no-contact toilet-paper standoff plan completed 40/40 stages at limits of 0.25 rad/s per joint and 0.05 m/s Cartesian speed.
- Reported minimum clearance was 0.07943 m, terminal position error was 0.001185 m, and final standoff was approximately 0.099942 m.
- A subsequent vertical-lift test rejected unsafe or invalid 20, 19, 15, and 14 cm candidates before actuation. The accepted 13 cm plan produced approximately 12.67 cm measured displacement.
- Safe-home completed, gravity float was confirmed, and all Midbrain/Test Agent processes and HTTP listeners were shut down.

These July 2026 trials predate spatial convention V2. Their reported
arm-base-axis behavior is legacy installation evidence, not a semantic
definition. The current software defines world +X front, +Y left, and +Z up
opposite gravity, then resolves that vector into arm-base coordinates through
a timestamped transform. The new contract and regression tests do not replace
physical cross-axis qualification. See
[Spatial Frame Convention](../contracts/14_spatial_frame_convention_v2.md).

The exact stopped publication regression passed `469/469`: 439 Python tests across all 62 published Python test files plus 30 Rust tests. The wider local tree passed another 109 vegetable-cutting tests, for a local-only total of `578/578`; that prototype is not part of the publication. Python compilation, JSON parsing, PowerShell parsing, clean-configuration checks, Rust formatting, and the Rust release build also passed.

The protected GitHub workflow retains its smaller legacy dependency and source-root set because updating it requires a publishing identity with the separate `workflow` OAuth scope. On its Linux host, its exact Python command passes 101 tests and skips thirteen: three Agents-SDK-only modules plus ten Windows named-shared-memory replay tests. This hosted baseline is intentionally reported separately from the complete independently run Windows publication matrix.

## Automated source checks completed during cleanup

The cleaned repository was validated with the source directories on `PYTHONPATH`:

- Orbbec calibration tests: 2 passed.
- Local VIO tests: 30 passed.
- Test Agent initialization and point-cloud tests: 5 passed.
- Total: 37 passed.

The tests cover six-position accelerometer solving, calibration persistence, sample-rate-independent initialization, inertial propagation, RGB-D correction behavior, IR fallback selection, gravity gating, reset/epoch behavior, observation sequence, initialization recovery, and point-cloud lifecycle.

## PowerShell validation command

```powershell
.\scripts\validate.ps1
```

The script first runs the clean-configuration contract audit. It then checks JSON and Python syntax, runs all Python tests, builds Python wheels into a temporary validation directory, checks Rust formatting/tests/release build when Cargo is available, and configures/builds CameraHost only when explicitly requested with valid Orbbec SDK paths.

The configuration-only audit is also available independently and does not start Providers:

```powershell
.\scripts\test_config_baselines.ps1
```

The repository workflow does not yet invoke the FoundationPose Provider suite. Run its validation separately from the repository root:

```powershell
.\providers\foundation_pose\scripts\validate_publication.ps1
```

The v0.3.0 publication passed 43 Provider tests and a live integration validator against the real Manager and Fabric. The live check confirmed Manager registration, capability discovery, status publication, and independent camera-to-Base and camera-to-Gripper transform edges.

The reBot arm Providers have separate offline validation entry points:

```powershell
.\providers\rebot_arm_dm\scripts\verify.ps1
.\providers\rebot_arm_integrated\scripts\verify.ps1
```

The Integrated suite verifies the Manager capability map, provider-local operation catalog, exclusion of experimental POS_VEL continuous and arm POS_TOR one-shot profiles, command construction, IK, gripper latching, Fabric staging, and non-motion safety behavior. These tests do not move the arm and do not replace physical acceptance testing.

## Hardware acceptance

### Startup

- Camera Provider becomes `HOT`.
- Auto-initialization reaches `SUCCEEDED`.
- Initialization blocker is `none`.
- Selected accelerometer and gyro windows meet the required count.
- Inferred sample rates are plausible.
- IMU propagation steps increase.
- Body pose is non-null.

### Rotation and correction

- Fast pivots continue updating orientation through inertial propagation.
- Visual correction can become stale without freezing pose.
- Reacquisition reduces drift without a large discontinuity.
- Gravity is disabled during obvious angular motion and becomes ready/active only after quiet stabilization.

### Mapping

- Map Capture becomes `CAPTURING` after pose and RGB-D are present.
- Point count increases.
- Forced reinitialization creates a new epoch and resumes capture.
- Old-epoch points are not mixed with the new world frame.

### Calibration

- All six orientations pass quality checks.
- The calibration file is backed up and replaced atomically.
- Provider reload applies the new revision.
- Corrected stationary acceleration is physically plausible.

## Platform-specific compile status

The Python source and tests are platform-independent. The Rust core can be built on Linux or Windows, but the operational release target is Windows MSVC. CameraHost is Windows- and Orbbec-SDK-dependent. A Linux cleanup environment cannot provide a hardware-valid CameraHost build.

## Remaining qualification

The current validation is not formal localization certification. Still required:

- deterministic synchronized recording/replay
- external trajectory ground truth
- absolute and relative trajectory error
- stationary velocity and position drift
- fast-pivot latency and orientation error
- visual-outage drift and reacquisition discontinuity
- camera/IMU time-offset estimation
- CPU and scheduling measurements on the target Windows system
- Base and Gripper pose repeatability against external 6D ground truth
- CAD-symmetry disambiguation and explicit ambiguity reporting
- mask perturbation, partial occlusion, lighting, and background-clutter trials
- long-duration tracking loss, reacquisition, and stale-transform behavior
- camera-alignment accuracy after stationary multi-sample aggregation
- MIT one-shot and continuous physical acceptance across representative unloaded poses
- POS_VEL one-shot acceptance only within the declared ≤20 cm/no-load envelope
- resolution of continuous POS_VEL instability before capability publication
- resolution of arm POS_TOR baseline/force instability before capability publication
- physical cross-axis qualification of the implemented semantic-direction
  resolver across camera, world, arm-base, controlled-frame, tool, and object
  frames
- transform age, revision, provenance, and uncertainty enforcement at the consuming Skill's decision boundary
- fault-injection validation for every authority handover and loss-of-upstream path
- structured effector-front and task-specific action-point VLM qualification across RGB/depth resolution and alignment mismatches

## Automatic Rust formatting

`validate.ps1` runs `cargo fmt --all` before the strict `cargo fmt --all -- --check` step. The first command applies the formatter from the installed Rust toolchain to every workspace crate, including platform-gated Windows code. The second command verifies that the resulting tree is fully formatted. Successful validation then refreshes all SHA-256 file manifests.
