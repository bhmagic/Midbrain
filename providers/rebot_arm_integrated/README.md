# Arm Integrated Free-Space Resource Provider

Integrated is the arm-agnostic free-space controller. It leases only the arm
actuator group from the hardware-facing Basic Provider and turns complete
Cartesian position or 6-DoF pose goals into controller-owned path plans and
exact signed commits. It owns IK, direct-path sampling, semantic collision
checks, trajectory timing, and local control auditing. Basic remains the final
authority for hardware transport, group fencing, motor limits, gravity
support, and safe-home.

Integrated does not own intentional contact, cutting, pushing, gripping, or
gripper actuation. Those roles require separate controllers and Skills. Arm
motion with a held object is also prohibited until a runtime attachment
revision supplies the object's transform, payload, and collision geometry.

## Execution boundary

The only free-space motion transaction is:

1. `POST /v1/motion/path-plan` creates a nonphysical immutable plan.
2. The host evaluates that exact plan under the autonomous no-contact policy.
3. `POST /v1/motion/path-commit` accepts one short-lived signed assertion bound
   to the plan, controller identity, configuration, assembly, and measured
   start.
4. `POST /v1/motion/path-release` releases an explicitly retained final state.

The plan request may select `execution_backend: IMPEDANCE` or
`execution_backend: POS_SPEED`. Omission means `IMPEDANCE`. The normalized
selection is included in `request_sha256`, so it cannot be changed after
preview. Both backends receive the same controller-paced 50 Hz joint-target
timeline; timing is recomputed against the Basic limits for the selected
command mode. The ordinary no-speed-request duration defaults to 1.5 seconds;
if that duration is shorter than the selected Basic limits permit, preview
stretches it to a feasible duration rather than exceeding a joint limit.

The Agent can invoke `perform_relative_effector_motion`, but it cannot select
or replay a plan identifier. Task-required Provider start, HOT, and WARM
transitions are autonomous. Normal signed free-space motion does not request a
human approval dialog.

Retired manual target staging, engagement, teleoperation, runtime-settings,
gripper, contact-baseline, and Fabric command-input routes are not exposed.
The Provider developer page is read-only except for gravity-float and safe
termination controls.

## Collision and route policy

- `PUSHABLE` geometry is ignored by the current temporary policy.
- `WORK_OBJECT` adds 0 mm of clearance, but intersection remains forbidden.
- `KEEP_OUT` adds 10 mm of clearance.
- Planning evaluates the direct Cartesian path. General obstacle rerouting is
  not implemented.
- When the destination is blocked, Integrated may execute the closest
  collision-free prefix and reports `CLOSEST_SAFE`.

Collision geometry comes from the central robot assembly selection. The arm
profile supplies its capsule chain; the selected mounted-effector profile
supplies controlled-frame collision spheres. The scene compiler, controller,
and main 3D viewer consume those same profile revisions.

Environment collision geometry remains sphere-based. In addition to the
legacy gripper/base ROI names, Integrated accepts the compiler's bounded
`HAND_ANGULAR_4PI` layer with its 5 mm minimum radius. The layer may contain up
to one sphere per occupied direction from a 4,096-direction hand-centric
projection; collision checking is unchanged because every projected hit is an
ordinary arm-base-frame sphere.

## Profiles and configuration

The machine-local central selection is
`config/robot_assemblies/primary_manipulator.json`. Its Provider-owned profile
references resolve through the selected arm Provider root, so installing a new
arm package requires only a small central selection change. Basic publishes
the resolved assembly state; Integrated binds its fingerprint into every
plan.

The machine-local controller configuration is
`providers/rebot_arm_integrated/config/controller.json`. Setup repairs it from
`config_templates/controller.default.json` and removes retired configuration
surfaces. Active configuration and runtime audit logs are not committed.

## Terminology

| Term | Meaning |
|---|---|
| `rebot_arm_base` | Canonical planning and semantic-scene frame |
| controlled frame | Assembly-profile frame whose pose Integrated controls |
| action point | Task semantic point; not a substitute controller API frame |
| `IMPEDANCE` | Default signed-path backend; Basic receives 50 Hz position and velocity targets plus a bounded target rate |
| `POS_SPEED` | Selectable signed-path backend name; Basic receives 50 Hz `POSITION_VELOCITY_LIMITED` position targets with per-command velocity limits |
| `POSITION_VELOCITY_LIMITED` | Basic command mode emitted when the signed plan selects `POS_SPEED` |

Current capability names are authoritative in `manifest.json` and the live
`GET /v1/capabilities` response.

## Documentation

- [Safety](docs/SAFETY.md)
- [Control architecture](docs/CONTROL_ARCHITECTURE.md)
- [Controller-owned path planning](docs/CONTROLLER_PATH_PLANNING.md)
- [Upstream integration](docs/UPSTREAM_INTEGRATION.md)
- [Physical qualification](docs/PHYSICAL_TEST.md)
- [Control audit](docs/CONTROL_AUDIT.md)
- [Validation](VALIDATION.md)
- [Changelog](CHANGELOG.md)
