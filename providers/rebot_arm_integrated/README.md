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
| `IMPEDANCE` | Basic cyclic impedance backend used by a signed path commit |
| `POSITION_VELOCITY_LIMITED` | Basic bounded endpoint backend used by a signed path commit |

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
