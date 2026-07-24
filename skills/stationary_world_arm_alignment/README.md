# Stationary World-Space Arm Finder

This finite Midbrain Skill aligns a stationary reBot arm base into a camera-origin world frame. All Skill code, environment, configuration, run artifacts, calibration revisions, schemas, tests, and the monitoring GUI live in this folder.

Read [docs/EVALUATION_AND_DESIGN.md](docs/EVALUATION_AND_DESIGN.md) for the algorithm, limitations, and safety model.

## Setup

Run `scripts\setup.ps1`. It creates `.venv` in this folder, installs the local Orbbec shared-memory reader into that environment, and installs the Skill.

The Skill reads `OPENAI_API_KEY` from the process or the workspace's existing `config\api_keys.env`. The default model is `gpt-5.6-luna`.

To override settings without changing this package, copy `config_templates\alignment.default.json` to `config\alignment.json` inside this Skill and pass its path in code. The default loader also accepts the existing workspace override path for compatibility, but all generated revisions remain in `config\calibrations` here.

## GUI

Run `scripts\run_gui.ps1`. The launcher:

- Reuses a healthy Midbrain Manager and Fabric, or starts both with the existing core launcher when both are stopped.
- Starts this Skill GUI in the background and opens `http://127.0.0.1:8011`.
- Automatically requests HOT residency for the RGB-D camera, local VIO, and basic arm-pose provider.
- Leaves FoundationPose stopped until base alignment requests it.

Use `scripts\run_gui.ps1 -NoBrowser` to suppress browser opening, or `-NoCoreStart` to require that Manager and Fabric already be running. A partially running core is reported rather than automatically restarting the healthy half, because a restart could disrupt unrelated providers. Stop only the GUI with `scripts\stop_gui.ps1`; the Midbrain core and requested input providers remain available to other work.

The monitor provides:

- Auto, FoundationPose-base + VLM-gripper, slow FoundationPose-base + FoundationPose-gripper for dim scenes, VLM-gripper-only adjustment, and cancel controls.
- Live Manager, Fabric, camera, VIO, arm, and on-demand FoundationPose readiness, plus a manual provider-request button.
- Direct FoundationPose health and session telemetry during multi-minute registration.
- A live ten-second VIO point-cloud trail with alignment frames and colored axes.
- Captured RGB/depth plus the live RGB with the projected base 3D box and XYZ arrows.
- The latest published calibration revision and transform summary.

## CLI

Use `scripts\run.ps1 -Mode foundation_base_vlm_gripper`, `scripts\run.ps1 -Mode foundation_base_gripper`, `scripts\run.ps1 -Mode vlm_gripper_only`, or the default `auto`. Auto selects `vlm_gripper_only` only when the prior calibration belongs to the current VIO epoch and its base is upright. Add `-ArmIsHome` when that assertion is useful. The Skill does not command the arm.

## Public mode and result contract

The manifest exposes these three concrete modes to upstream Skills:

| Mode | Base source | Primary gripper source | Intended use |
|---|---|---|---|
| `foundation_base_gripper` | FoundationPose base pose | FoundationPose gripper pose | Slow, dim-scene path using both CAD models |
| `foundation_base_vlm_gripper` | FoundationPose base pose | VLM RGB-D foremost-beak point | Faster default base registration |
| `vlm_gripper_only` | Prior alignment with rotation locked | VLM RGB-D foremost-beak point | Later translation adjustment without starting FoundationPose |

`auto` selects one of the latter two modes and publishes the selected concrete mode. The old `vlm_refine` API value remains a hidden compatibility alias and is canonicalized to `vlm_gripper_only`; new callers should not use it.

Result schema version 2 publishes `mode_contract`, `gripper_measurements`, and `gripper_cross_source_comparison`. Every gripper measurement identifies its `source_type`, physical `semantic_point`, coordinate frame, position, and role. `VLM_RGBD_BEAK` measures the foremost beak mean, while `FOUNDATIONPOSE_GRIPPER_POSE` measures the gripper model origin. They are intentionally not declared directly comparable; an upstream Skill must apply calibrated tool geometry before treating their difference as an adjustment.

## Validation

Run `scripts\check.ps1` for compilation and unit tests. Hardware validation should first use an empty, stationary workspace, then compare the published arm-base transform against a measured fiducial or known contact point before allowing downstream motion planning to trust it.

## Runtime files

- `run\<alignment-id>` contains the exact RGB/depth evidence, masks, pose overlays, and VLM validation JSON for a run.
- `config\calibrations\<alignment-id>.json` is an immutable revision.
- `config\calibrations\latest.json` is the current local pointer.

The projected-pose composite is also published to Fabric on `skills.stationary_world_arm_alignment.pose_overlay`; its data contains the GUI image address and immutable local artifact path.

The RGB-D capture path copies each high-rate bundle immediately after fetching it. If the shared-memory slot is recycled, it waits for and fetches a fresh bundle rather than retrying the expired reference.

FoundationPose is stopped after the Skill when no foreign active sessions remain. Camera, VIO, and the arm pose source are left running for upstream consumers.
