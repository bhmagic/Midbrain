# reBot Arm DM Basic Resource Provider

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
| `VELOCITY` | `VEL` | No reviewed Integrated profile | Continuous velocity command; excluded from the calibration GUI |
| `POSITION_EFFORT_LIMITED` | `FORCE_POS` | `CONTACT_WORK` / `POS_TOR` | Latched position endpoint with velocity and effort-ratio limits |

`POS_TOR` is not an additional Basic command mode: it is Integrated terminology
that maps to Basic `POSITION_EFFORT_LIMITED`, whose MotorBridge mode name is
`FORCE_POS`. `POS_SPEED` names a speed-cap policy in the current Integrated
controller; it is not a motor mode and must not be substituted for `POS_VEL`
in Basic requests.

See [Motor command semantics](docs/MOTOR_COMMAND_OVERWRITE_SEMANTICS.md) for
endpoint replacement, keepalive, and mode-transition behavior.

## Environment and configuration

Basic owns a Provider-local Python environment at
`providers/rebot_arm_dm/.venv`. Create it with
`scripts/setup.ps1 -WithMotorBridge`; it is not shared with Integrated and is
not committed to Git.

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
- fenced lease acquisition, renewal, expiry, and revocation;
- calibrated arm and declared-payload gravity feed-forward;
- gravity-float, fault response, and safe-home sequencing; and
- bounded calibration primitives and machine-local calibration output.

Basic exposes motor primitives and hard safety enforcement. The Integrated
Provider owns reviewed Cartesian profiles, path previews, semantic-scene
checks, and higher-level physical release policy.

## Documentation

Human and installation-agent entry points:

- [Windows setup and bring-up](docs/WINDOWS_INSTALL_COMPILE_RUN.md) — install,
  simulation, first read-only connection, operation, and shutdown.
- [Safety behavior](docs/SAFETY.md) — load-bearing stiffness, gravity support,
  calibration motion, failures, and safe-home invariants.
- [Calibration](docs/CALIBRATION.md) — attended friction-calibration workflow
  and the fitted model.
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
