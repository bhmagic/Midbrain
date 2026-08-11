# HOT Arm Semantic Scene Compiler

`world_model.arm_scene_compiler` is the single runtime owner of the canonical
semantic sphere scene consumed by the Integrated arm controller. HOT residency
has a material advantage: the Provider repeatedly reacquires expiring camera
BufferRefs, applies timestamped transforms, rebuilds current robot
self-exclusion geometry, merges slower upstream semantic assertions, and
publishes a short-lived monotonic scene revision. WARM residency retains the
process but stops compilation and publication; COLD stops the process.

## Scene contract

The output stream is `robot_arm.primary.integrated.scene` using schema
`physical_agent.arm_semantic_sphere_scene`, schema version 1, contract version
2, and frame `rebot_arm_base`. Continuously refreshed compiled scenes carry a
10-second bounded validity horizon so a controller preview can survive one
Agent tool handoff; this does not change the faster tracker/compiler update
rate or permit missing required `KEEP_OUT` coverage.

- `GRIPPER_0P5M` covers 0.5 m around the measured tool center and has a 20 mm
  minimum sphere radius.
- `ARM_BASE_1P2M` covers 1.2 m around the arm-base origin and has a 60 mm
  minimum sphere radius.
- The fresh `robot_arm.assembly_state` selects the arm capsule profile and the
  mounted-effector sphere profile. Both are transformed at the observation
  timestamp and sampled into one profile-bound self-exclusion filter before
  point-cloud voxelization or SAM2 semantic-cell publication.
- Unclaimed point-cloud geometry is non-blocking `PUSHABLE` telemetry and is
  omitted from controller geometry by default. Only explicitly described
  `KEEP_OUT` assertions become blocking spheres.
- `PUSHABLE` and `WORKPIECE`/`WORK_OBJECT` require explicit upstream semantic
  assertions. A successful metric `locate_item` result publishes a five-second
  `WORKPIECE` assertion through Fabric.
- A material-limited or empty depth cloud may produce `SEMANTIC_ONLY` output
  only while a fresh explicit semantic assertion exists. The Provider reports
  `DEGRADED`, and unobserved obstacles remain unknown. With neither geometry
  nor an assertion, it refuses to publish rather than pretending the space is
  clear.

Fabric validates the canonical upstream schemas
`physical_agent.arm_point_cloud` and
`physical_agent.arm_semantic_assertions`. This lets outside Skills or agents
contribute observations without importing the Provider package. The default
streams are `robot_arm.scene.point_cloud` and
`robot_arm.scene.semantic_assertions`; the native camera
`camera.point_cloud.xyz.frame_ref` remains the high-rate fallback.

## Runtime dependencies

HOT compilation requires all of the following to be fresh at the same time:

- a configured point-cloud stream;
- a timestamped transform from the point-cloud frame to `rebot_arm_base`;
- the current Basic `robot_arm.assembly_state`, including its assembly
  fingerprint, arm collision profile, and mounted-effector profile;
- the current Basic transform chain for every collision-profile frame and the
  mounted controlled frame; and
- for semantic-only depth fallback, at least one unexpired explicit semantic
  assertion.

The reviewed mounted camera-to-`rebot_arm_base` registration is identity- and
calibration-gated rather than wall-clock-expired. Temporary camera loss
suspends scene production; recovery with the same canonical camera device,
calibration, and reviewed activation restores it without requiring a Local VIO
epoch. Camera identity or calibration changes require a new reviewed
registration. Fast gripper-based translation refinement is used
after the initial base calibration has learned tool-to-beak geometry.

## Setup and inspection

Run `providers\arm_scene_compiler\scripts\setup.ps1`. If the Windows `py`
launcher has no registered Python, pass an existing Python 3.11 executable as
`-PythonLauncher`; this workspace was bootstrapped successfully from
`test_agent\.venv\Scripts\python.exe`.

The finite Agent tool `inspect_arm_semantic_scene` is the supported test
surface. It reports producer identity, freshness, scene revision, both ROI
policies, counts by type and scope, depth mode, and an optional bounded sphere
list. It is read-only and never authorizes movement. A practical test order is:

1. Establish a fresh camera-to-`rebot_arm_base` registration.
2. Keep the camera and Basic arm Providers HOT, without commanding motion.
3. Set `world_model.arm_scene_compiler` HOT.
4. Run `locate_item` for the toilet-paper roll in `rebot_arm_base`; a metric
   result publishes its short-lived workpiece assertion.
5. Run `inspect_arm_semantic_scene` and require `SCENE_READY`, both ROI layers,
   nonzero sphere counts, and one `WORK_OBJECT` for the roll.
6. Confirm the Integrated controller reports the same exact scene revision as
   accepted before relying on a collision-aware preview.

The Provider control endpoints are read-only diagnostics at `/health`,
`/v1/status`, `/v1/scene`, and `/v1/diagnostics`; lifecycle controls use the
standard `/v1/control/hot`, `/warm`, `/stop`, and `/request` paths.

## Documentation

- [Changelog](CHANGELOG.md) — release history; not an operating procedure.
