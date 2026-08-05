# reBot Arm Integrated Resource Provider

The Integrated Provider leases the hardware-facing Basic Provider and turns
Cartesian targets into reviewed arm-control profiles. It owns inverse
kinematics, controller-owned path previews, semantic-scene collision checks,
profile-specific execution, gripper coordination, and local control auditing.
Basic remains the final authority for motor transport, leases, joint and motor
limits, load-bearing control, gravity support, and safe-home.

Operator testing and autonomous execution use separate authority boundaries:

- local physical testing requires the Provider's documented GUI engagement
  and gamepad input;
- agentic transit requires an exact nonphysical preview plus a signed,
  short-lived, decision-specific, one-time authorization assertion;
- Fabric targets, capability discovery, scene observations, and previews never
  grant physical motion authority by themselves.

`manifest.json` and the live `GET /v1/capabilities` response are authoritative
for current capability names and maturity. Package release identity is not
publishable until `VERSION`, manifest, Python metadata, and runtime version
surfaces agree; repository-wide normalization is tracked in the
[active roadmap](../../docs/09_LIMITATIONS_AND_ROADMAP.md#normalize-package-and-contract-versions).
This README explains the stable component boundary rather than copying the
full operation catalog.

## Control terminology

Integrated profile names, Basic API modes, and Damiao/MotorBridge names are
related but not interchangeable:

| Integrated profile | Canonical Basic API mode | Motor/device alias | Meaning |
|---|---|---|---|
| `PRESS_MIT` | `IMPEDANCE` | MIT | Cyclic impedance trajectory |
| `TRANSIT_SPEED` | `POSITION_VELOCITY_LIMITED` | `POS_VEL` | Latched position endpoint with a velocity limit |
| `CONTACT_WORK` | `POSITION_EFFORT_LIMITED` | `FORCE_POS` | Latched position endpoint with velocity and effort-ratio limits |

Integrated historically calls the CONTACT_WORK backend `POS_TOR`; that alias
maps to Basic `POSITION_EFFORT_LIMITED` and the motor-adapter `FORCE_POS` mode.
`POS_SPEED` describes the policy used to choose a speed ceiling; it is not a
fourth motor mode and must not replace `POS_VEL` in a Basic command.

[Control architecture](docs/CONTROL_ARCHITECTURE.md) owns execution-profile
semantics. [Basic motor command semantics](../rebot_arm_dm/docs/MOTOR_COMMAND_OVERWRITE_SEMANTICS.md)
owns endpoint overwrite, keepalive, and motor-mode transition behavior.

## Spatial terminology

| Term | Meaning |
|---|---|
| `rebot_arm_base` | Canonical controller planning and scene frame |
| `rebot_arm_tool` | Basic's measured terminal tool frame |
| controlled frame | The frame whose target pose Integrated solves and controls; it may be offset from `rebot_arm_tool` |
| `ik_offset` | Tool-to-controlled-frame transform applied once by Integrated |
| action point | Task-level semantic point associated with a tool; not a substitute API name for the controlled frame |
| arm root | Visual/calibration semantic term used by some model profiles; it is not a controller frame identifier until a reviewed alignment binds it to `rebot_arm_base` |

Upstream callers provide the desired controlled-frame pose and must not
pre-apply `ik_offset`. Doing so would apply the offset twice. See
[Upstream integration](docs/UPSTREAM_INTEGRATION.md) for the command boundary
and [the active arm-root alignment plan](../../docs/13_GRIPPER_MOTION_ARM_ROOT_ALIGNMENT.md)
for the unfinished calibration relationship.

## Environment and configuration

Integrated owns `providers/rebot_arm_integrated/.venv`, created by
`scripts/setup.ps1`. It does not share Basic's environment. Setup seeds a
missing machine-local `config/controller.json` from
`config_templates/controller.default.json`; active configuration and runtime
audit logs are not committed.

If Basic fences or revokes the operational lease, Integrated enters
`RECOVERY_REQUIRED`, stops background lease acquisition, and becomes not
ready. A new lease is attempted only after an explicit HOT transition once the
Basic safety operation has completed.

Read [Safety](docs/SAFETY.md) before physical use, then follow the bounded
[physical test procedure](docs/PHYSICAL_TEST.md). Use
[Validation](VALIDATION.md) to distinguish stopped software evidence from
guarded physical qualification.

## Documentation

Human and installation-agent entry points:

- [Safety](docs/SAFETY.md) — authority boundaries, fallback behavior,
  payload assumptions, and safe termination.
- [Physical test procedure](docs/PHYSICAL_TEST.md) — attended bring-up order
  and exact gamepad mapping.
- [Validation](VALIDATION.md) — current stopped coverage and remaining
  physical qualification.

Coder and coding-agent references:

- [Control architecture](docs/CONTROL_ARCHITECTURE.md) — ownership,
  execution-profile semantics, scene policy, and torque-baseline model.
- [Upstream integration](docs/UPSTREAM_INTEGRATION.md) — capability discovery,
  HTTP operations, Fabric command schema, acknowledgements, and retries.
- [Controller-owned path planning](docs/CONTROLLER_PATH_PLANNING.md) —
  nonphysical preview and separately authorized signed commit.
- [Provider-local control audit](docs/CONTROL_AUDIT.md) — synchronous local
  audit and asynchronous Fabric copy.
- [`manifest.json`](manifest.json) — authoritative identity, capabilities,
  profile metadata, and readiness declarations.

History:

- [Changelog](CHANGELOG.md) — historical implementation changes; not current
  operating or capability guidance.
