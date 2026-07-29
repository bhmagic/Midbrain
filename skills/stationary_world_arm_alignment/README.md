# Stationary World-Space Arm Finder

This finite Midbrain Skill aligns a stationary reBot arm base into a camera-origin world frame. All Skill orchestration, configuration, run artifacts, calibration revisions, schemas, tests, and the monitoring GUI live in this folder.

See `CHANGELOG.md` for the reviewed-activation, Agent discovery, GUI, and
hardware-validation changes made during the 2026-07-29 system test.

Version 0.6 adds a versioned identical-observation comparison contract and
closes the `SKILL_LOCAL` backend after every finite calibration attempt before
its samples can be returned. Version 0.5 added the non-default `SKILL_LOCAL`
base-pose engine route. It invokes a FoundationPose-compatible estimator inside
the finite calibration operation, consumes fresh RGB-D frames directly, and
produces the same sample contract as the existing Provider route.
`PROVIDER_COMPATIBILITY` remains the default until the local route has been
compared in the guarded hardware environment.
Automatic fallback is disabled so a local failure cannot silently become a
multi-minute Provider run.

The version 0.6 comparison contract is implemented in
`stationary_world_arm_alignment.route_comparison`. A route-run record binds its
result to exact capture hashes, camera route and boot identity, calibration
revision, VIO epoch, timestamp, and gripper configuration. A comparison passes
only when both routes used the identical observation, remained nonphysical,
released owned sessions/GPU resources, met repeatability thresholds, and
agreed within configured translation and rotation limits. Comparison evidence
is explicitly not motion-usable.

Recorded Phase 5 replay manifests can be converted into one verified comparison
observation with `load_replay_observation`. It checks the actual RGB and
registered-depth bytes against their recorded SHA-256 values, rejects paths
outside the bundle, and includes provider instance plus replay provenance in
the common fingerprint.

Read [docs/EVALUATION_AND_DESIGN.md](docs/EVALUATION_AND_DESIGN.md) for the algorithm, limitations, and safety model.

## Setup

Run `scripts\setup.ps1`. It creates `.venv` in this folder, installs the local Orbbec shared-memory reader into that environment, and installs the Skill.

The Skill reads `OPENAI_API_KEY` from the process or the workspace's existing `config\api_keys.env`. The default model is `gpt-5.6-luna`.

To override settings without changing this package, copy `config_templates\alignment.default.json` to `config\alignment.json` inside this Skill and pass its path in code. The default loader also accepts the existing workspace override path for compatibility, but all generated revisions remain in `config\calibrations` here.

## GUI

Run `scripts\run_gui.ps1`. The launcher:

- Reuses a healthy Midbrain Manager and Fabric, or starts both with the existing core launcher when both are stopped.
- Starts this Skill GUI in the background and opens `http://127.0.0.1:8011`.
- Starts passively. Opening the debugging GUI does not request camera, VIO, or
  arm residency; use **Request providers** when acquisition is intended.
- Leaves the FoundationPose compatibility Provider stopped until a base
  alignment explicitly using that route requests it. The `SKILL_LOCAL` route
  does not start the Provider.

Use `scripts\run_gui.ps1 -NoBrowser` to suppress browser opening, or `-NoCoreStart` to require that Manager and Fabric already be running. A partially running core is reported rather than automatically restarting the healthy half, because a restart could disrupt unrelated providers. Stop only the GUI with `scripts\stop_gui.ps1`; the Midbrain core and requested input providers remain available to other work.
Legacy automatic input acquisition can be restored explicitly with
`MIDBRAIN_GUI_AUTO_BOOTSTRAP_PROVIDERS=true`.

The monitor provides:

- Auto, FoundationPose-base + VLM-gripper, slow FoundationPose-base + FoundationPose-gripper for dim scenes, VLM-gripper-only adjustment, and cancel controls.
- Live Manager, Fabric, camera, VIO, arm, and on-demand FoundationPose readiness, plus a manual provider-request button.
- Direct FoundationPose health and session telemetry during multi-minute registration.
- A live ten-second VIO point-cloud trail with alignment frames and colored axes.
- Captured RGB/depth plus the live RGB with the projected base 3D box and XYZ arrows.
- The latest published calibration revision and transform summary.
- A calibration-candidate review surface. It shows exact digest, expiry,
  camera boot/calibration, VIO epoch, and prior decision state.

Candidate decisions are separate append-only records in
`config\calibration_reviews`. Approval means
`APPROVED_FOR_ACTIVATION`; it does not activate or publish a transform and
always retains `motion_usable=false`. Decisions reject stale candidates,
changed digests, incomplete or changed provenance, conflicting idempotency
keys, and unverified identity. The browser is fail-closed unless a trusted
external identity service is configured with a decision-scoped signed
assertion.

A reviewed candidate becomes motion-usable only through Manager
`POST /v1/workcell-calibrations/activate`. Manager verifies the exact
candidate digest and signed reviewer identity, current camera
provider/instance/boot/calibration, current tracking VIO epoch, frame contract,
quality bounds, and expiry before publishing the active transform. Only one
unexpired activation may be active. Use
`POST /v1/workcell-calibrations/{activation_id}/revoke` to publish an
authoritative non-motion-usable revocation; a later static observation cannot
silently revive the older edge.

## CLI

Use `scripts\run.ps1 -Mode foundation_base_vlm_gripper`, `scripts\run.ps1 -Mode foundation_base_gripper`, `scripts\run.ps1 -Mode vlm_gripper_only`, or the default `auto`. Auto selects `vlm_gripper_only` only when the prior calibration belongs to the current VIO epoch and its base is upright. Add `-ArmIsHome` when that assertion is useful. The Skill does not command the arm.

For an in-session multimodal reviewer such as Codex, invoke the Python entry
point with `--vision-route REVIEWED_FILE --review-timeout-s 300`. The Skill
writes an exact-evidence request and waits for a schema-checked response in the
same run directory. Every response must bind the request SHA-256 and identify
the reviewer. This route has no automatic API fallback and fails closed on a
missing, stale, malformed, or mismatched response.

## Public mode and result contract

The manifest exposes these three concrete modes to upstream Skills:

| Mode | Base source | Primary gripper source | Intended use |
|---|---|---|---|
| `foundation_base_gripper` | FoundationPose base pose | FoundationPose gripper pose | Slow, dim-scene path using both CAD models |
| `foundation_base_vlm_gripper` | FoundationPose base pose | VLM RGB-D foremost-beak point | Faster default base registration |
| `vlm_gripper_only` | Prior alignment with rotation locked | VLM RGB-D foremost-beak point | Later translation adjustment without starting FoundationPose |

`auto` selects one of the latter two modes and publishes the selected concrete mode. The old `vlm_refine` API value remains a hidden compatibility alias and is canonicalized to `vlm_gripper_only`; new callers should not use it.

Result schema version 3 retains `mode_contract`, `gripper_measurements`, and
`gripper_cross_source_comparison`, and adds an expiring, non-motion-usable
version-2 calibration candidate with an explicit world/VIO/arm-base frame
contract, estimator, error-bound, camera-route, BufferRef, calibration, and
VIO provenance. Every gripper measurement identifies its
`source_type`, physical `semantic_point`, coordinate frame, position, and role.
`VLM_RGBD_BEAK` measures the foremost beak mean, while
`FOUNDATIONPOSE_GRIPPER_POSE` measures the gripper model origin. They are
intentionally not declared directly comparable; an upstream Skill must apply
calibrated tool geometry before treating their difference as an adjustment.

New version-2 results also publish the backward-compatible optional
`vio_from_camera_reference` full pose captured at the alignment RGB-D
timestamp. A downstream fixed-camera Skill should use this saved translation
and rotation instead of a later live VIO pose, then it may stop VIO for its own
finite session. Older version-2 results without this field require an explicit
legacy fallback or a new alignment.

## Validation

Run `scripts\check.ps1` for compilation and unit tests. Hardware validation should first use an empty, stationary workspace, then compare the published arm-base transform against a measured fiducial or known contact point before allowing downstream motion planning to trust it.

Base-pose validation allows two fresh FoundationPose attempts. A strict pass
returns immediately. If both miss the strict threshold, the Skill ranks them
by geometric reasonableness, box fit, orientation fit, confidence, and visible
projection coverage. The better attempt is accepted only when its box and
orientation remain non-BAD and it clears the configured fallback confidence;
otherwise its overlay is retained for diagnosis while the alignment stays
invalid.

## Runtime files

- `run\<alignment-id>` contains the exact RGB/depth evidence, masks, pose overlays, and VLM validation JSON for a run.
- `config\calibrations\<alignment-id>.json` is an immutable revision.
- `config\calibrations\latest.json` is the current local pointer.
- `config\calibration_reviews\<candidate-id>.json` is an immutable review
  decision that never activates motion.

The projected-pose composite is also published to Fabric on `skills.stationary_world_arm_alignment.pose_overlay`; its data contains the GUI image address and immutable local artifact path.

The RGB-D capture path copies each high-rate bundle immediately after fetching it. If the shared-memory slot is recycled, it waits for and fetches a fresh bundle rather than retrying the expired reference.

On the compatibility route, FoundationPose is stopped after the Skill when no
foreign active sessions remain. On the Skill-local route there is no Provider
session to stop. Camera, VIO, and the arm pose source are left running for
upstream consumers.
