# reBot Arm DM Basic Resource Provider

The recommended managed launch selects the installed robot through
`config/robot_assemblies/primary_manipulator.json`. That machine-local file
references the model, calibration, mounted-effector, and collision profiles
inside this Provider. Direct `--config` and `--calibration` arguments remain a
legacy/development fallback.

The Basic Provider is the sole hardware-facing owner of the reBot/Damiao motor
transport. It publishes seven-motor feedback and local transforms, enforces
fenced operational leases and command deadlines, validates motor modes and
limits, maintains gravity support, and owns the authoritative safe-home
termination path.

It deliberately does not own Cartesian planning, desktop geometry, semantic
obstacle interpretation, or task policy. Those responsibilities belong to
higher-level Providers and finite Skills. They submit commands through the
Basic contract; they must never open the motor transport directly.

`manifest.json` is authoritative for Provider identity, version, capabilities,
and readiness metadata. The command schema is authoritative for accepted API
mode strings. Documentation explains their meaning and safety boundary.

## Command terminology

Three vocabularies appear in the stack. Use the canonical Basic API term in
schemas, requests, and implementation discussions; use a shorter alias only
when discussing the named device or Integrated profile.

| Canonical Basic API mode | Damiao/MotorBridge name | Integrated profile or UI name | Meaning |
|---|---|---|---|
| `IMPEDANCE` | MIT | `PRESS_MIT` | Cyclic position, velocity, stiffness, damping, and feed-forward torque setpoint |
| `POSITION_VELOCITY_LIMITED` | `POS_VEL` | `TRANSIT_SPEED` | Latched position endpoint with a velocity limit |
| `VELOCITY` | `VEL` | No reviewed Integrated profile | Continuous velocity command; excluded from the Hardware Development UI |
| `POSITION_EFFORT_LIMITED` | `FORCE_POS` | `CONTACT_WORK` / `POS_TOR` | Latched position endpoint with rad/s velocity and N·m torque limits |

`POS_TOR` is not an additional Basic command mode: it is Integrated terminology
that maps to Basic `POSITION_EFFORT_LIMITED`, whose MotorBridge mode name is
`FORCE_POS`. `POS_SPEED` names a speed-cap policy in the current Integrated
controller; it is not a motor mode and must not be substituted for `POS_VEL`
in Basic requests.

The public `POSITION_EFFORT_LIMITED` command field is `torque_limit_nm`.
`/v1/arm/model` publishes effective per-joint boundaries under
`command_limits.IMPEDANCE`, `command_limits.POSITION_VELOCITY_LIMITED`, and
`command_limits.POSITION_EFFORT_LIMITED`. Higher providers consume these
mode-specific Basic-owned limits rather than calibration or developer-test
fields. MotorBridge's FORCE_POS ratio is an adapter-private representation
produced only inside Basic.

See [Motor command semantics](docs/MOTOR_COMMAND_OVERWRITE_SEMANTICS.md) for
endpoint replacement, keepalive, and mode-transition behavior.

## Environment and configuration

Basic owns a Provider-local Python environment at
`providers/rebot_arm_dm/.venv`. Create it with
`scripts/setup.ps1 -WithMotorBridge`; it is not shared with Integrated and is
not committed to Git. Hardware setup builds the pinned reviewed MotorBridge
source with the tracked additive feedback-generation/receive-age patch; Basic
fails closed if that API is absent.

Setup seeds missing machine-local model, calibration, and calibration-collision
files from `config_templates`. Active files under `config` are installation
state and must not be committed or overwritten during an update.

Follow [Windows setup and bring-up](docs/WINDOWS_INSTALL_COMPILE_RUN.md) for
installation, stopped validation, simulation, read-only hardware connection,
and normal shutdown. Read [Safety behavior](docs/SAFETY.md) before opening a
real motor connection.

## Responsibility summary

Basic owns:

- exclusive motor-bus access and hardware identity checks;
- measured feedback and local arm transforms;
- command-schema, joint, rate, stiffness, damping, effort, and deadline checks;
- fenced root or disjoint actuator-group lease acquisition, renewal, expiry,
  and revocation;
- calibrated arm and declared-payload gravity feed-forward;
- gravity-float, explicit Manager `HOT` fault requalification, and safe-home
  sequencing;
- repeated-stop termination confirmation based on fresh measured non-movement,
  independent of absolute joint position, plus explicit process release when
  the controller has already lost control; and
- bounded attended manual-test primitives. Basic consumes a reviewed
  calibration profile but does not generate or modify it.

A control fault never restores motion authority automatically. A later Manager
`HOT` request may recover only after Basic has received a recent complete
generation-verified joint batch. Recovery fences any earlier lease and enters
powered gravity float; a higher-level controller must acquire new authority
before it can move the arm again.

The selected assembly defines disjoint arm and effector joint membership.
Basic rejects every group command that includes a joint outside that group.
The arm and gripper groups may be leased concurrently, while a root lease
conflicts with both. This permits a separate grip controller to retain its
hold while the free-space controller moves the arm.

At the service boundary, an omitted `resource_id` and the configured canonical
root ID both select root authority. Only a declared child resource selects
actuator-group authority. Lease responses include their canonical resource ID,
which clients may round-trip unchanged through renew, command, payload,
gravity-float, and release requests.

Basic exposes motor primitives and hard safety enforcement. The Integrated
free-space Provider owns reviewed Cartesian profiles, path previews,
semantic-scene checks, and higher-level physical release policy.

The static mounted effector comes from the selected Provider-owned profile.
That profile also owns its collision primitives. The arm collision profile is
arm-only, allowing the central assembly selection to pair the same arm with a
different gripper or fixed tool without copying effector geometry into the arm
profile.
It also owns the effector inertial mass and center of mass used by Basic's
gravity model. The checked-in development `5 inch blade` profile currently
records a total effector mass of 0.33 kg and a center of mass of
`[-0.165, 0.0, -0.03]` m in `end_link`. Its temporary collision envelope is
expressed in `rebot_arm_tool` as four spheres:

- center `[-0.005, 0.0, -0.07]` m, radius 0.005 m;
- center `[-0.03, 0.0, -0.07]` m, radius 0.015 m;
- center `[-0.09, 0.0, -0.07]` m, radius 0.035 m; and
- center `[-0.15, 0.0, -0.07]` m, radius 0.035 m.

These are operator-tuned development values, not metrology measurements or a
physical qualification of the complete operating envelope. A later inertial
or geometry change must create a new profile revision and repeat the applicable
gravity-float and free-space tests. Profile edits require Basic and its higher
dependents to restart.

A fixed-tool profile can list unavailable model joints in
`inactive_joint_names`. Basic excludes them from the six-axis arm group without
advertising a nonexistent gripper actuator resource. It also omits those
motors from MotorBridge registration, enable, feedback, and command traffic.
The legacy seven-slot state marks the unavailable slot
`INACTIVE_NOT_INSTALLED`; only installed motors determine feedback freshness.
An object later held by a gripper is not part of that file: it requires a
separate runtime attachment revision with payload and collision geometry.
That runtime registry is not implemented yet, so a higher controller must not
claim collision-safe free-space motion while an undeclared object is held.

## Documentation

Human and installation-agent entry points:

- [Windows setup and bring-up](docs/WINDOWS_INSTALL_COMPILE_RUN.md) — install,
  simulation, first read-only connection, operation, and shutdown.
- [Safety behavior](docs/SAFETY.md) — load-bearing stiffness, gravity support,
  attended development motion, failures, and safe-home invariants.
- [Hardware Development UI](docs/DEVELOPMENT_UI.md) — bounded manual joint
  testing, local collision diagnostics, and the explicit non-calibration
  boundary.
- [Validation](VALIDATION.md) — stopped coverage and physical checks that
  remain outstanding.

Coder and coding-agent references:

- [Architecture](docs/ARCHITECTURE.md) — ownership boundary, states, timing,
  and physical command modes.
- [Motor command semantics](docs/MOTOR_COMMAND_OVERWRITE_SEMANTICS.md) —
  canonical/alias mapping, latest-envelope behavior, endpoint keepalive, and
  transitions.
- [Upstream references](docs/OFFICIAL_REFERENCES.md) — source revisions,
  motor specifications, unresolved upstream conflicts, and interpretation
  limits.
- [`manifest.json`](manifest.json) and
  [`robot_arm_command.schema.json`](schemas/robot_arm_command.schema.json) —
  authoritative machine-readable interface.

History and compliance:

- [Changelog](CHANGELOG.md) — release history; not current operating guidance.
- [Notice](NOTICE.md) — upstream and redistribution boundary.
