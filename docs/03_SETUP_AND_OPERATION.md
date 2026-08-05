# Setup and Operation

This is the canonical operator path for the current Windows reference system.
Component development and physical qualification require the additional
instructions beside each Provider.

## Requirements

The complete camera and VIO path targets Windows 10/11 and requires:

- Developer PowerShell for Visual Studio 2022 with the C++ workload;
- stable Rust MSVC with `cargo` and `rustfmt`;
- Python 3.11;
- CMake;
- Orbbec SDK 2.8.6 development and runtime files; and
- an Orbbec Femto Bolt.

The optional reBot arm path also requires the supported seven-motor assembly,
reviewed machine-local calibration, Windows serial access, and
`motorbridge>=0.4.9`. The Xbox-compatible controller is needed only for the
manual Integrated development GUI.

The optional FoundationPose route requires an NVIDIA/CUDA/PyTorch environment,
the pinned upstream runtime, Git LFS checkpoints, and any separately installed
initialization dependencies. Its third-party license restrictions apply.

The Orbbec SDK, MotorBridge, upstream FoundationPose runtime, virtual
environments, API keys, and measured device calibration are not distributed as
ordinary repository source.

## Workspace and local state

The scripts support any normal writable workspace path. In examples,
`<MIDBRAIN_ROOT>` means the repository root; do not copy a developer-specific
absolute path into configuration or documentation.

Machine-local state belongs under `config` and ignored component runtime
directories. Do not commit API keys, signing secrets, serial identities,
measured calibration, captures, logs, SQLite runtime records, or active
Provider configuration.

## First setup

Open Developer PowerShell in the repository root:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\platform_core\scripts\setup_workspace.ps1
```

The setup builds Manager and Fabric, creates component-owned Python
environments, builds CameraHost when its SDK paths are available, installs the
core Providers and Skills, and creates missing local configuration from blank
examples without overwriting existing files.

There is no repository-root Python environment. Each Python process launches
from the environment owned by its component.

Set up the arm Providers separately:

```powershell
.\providers\rebot_arm_dm\scripts\setup.ps1 -WithMotorBridge
.\providers\rebot_arm_integrated\scripts\setup.ps1
.\providers\rebot_arm_dm\scripts\register.ps1
.\providers\rebot_arm_integrated\scripts\register.ps1
```

Set up the retained FoundationPose paths only when required:

```powershell
git lfs pull
.\providers\foundation_pose\scripts\setup.ps1
.\skills\stationary_world_arm_alignment\scripts\setup.ps1
```

These commands make the explicit finite initializer and compatibility Provider
available. They do not make FoundationPose the default alignment path or an
automatic fallback.

## Start Midbrain

For normal operation, double-click `Start Midbrain.cmd`. It starts Manager,
Fabric, and the idle Agent UI, then opens:

`http://127.0.0.1:7001/`

The portal is observation-first. Opening it does not activate hardware,
execute a Skill, reset a spatial epoch, or authorize motion. Providers remain
`COLD` until a guarded operator or Agent workflow requests them.

For automation or recovery:

```powershell
.\platform_core\scripts\run_workspace.ps1
```

Useful options include `-NoBrowser`, `-StartAgentUi`, `-CoreOnly`, and
`-AllowProviderAutoStart`. Normal desktop startup deliberately ignores old
machine-local `auto_start: true` entries unless that last option is supplied.

## Use the portal

Check Manager and Fabric before relying on component state. Provider cards
distinguish:

- process liveness;
- `COLD`, `WARM`, and `HOT` residency;
- component health;
- per-capability readiness;
- observation freshness and source identity; and
- active work.

A running or `HOT` Provider can still be unready, unhealthy, stale, or missing
an optional capability. Open its observation page before entering a
development UI.

Development UIs may expose administrative or physical controls beyond an
ordinary Agent workflow. The portal presents a warning and guarded activation
step before opening them. Finite-Skill development links may start an
inspection host; they do not execute the Skill itself.

## Use the Agent

The regular and developer pages are two projections of one backend-owned
autonomous Agent runtime. The developer view adds diagnostics; it does not add
authority or bypass controller checks.

For a supported physical action, the intended boundary is:

1. inspect current Manager, Provider, and Fabric state;
2. make required Providers ready through lifecycle policy;
3. collect coherent evidence;
4. produce a nonphysical controller preview;
5. resolve policy or a development authorization for that exact preview;
6. execute through the controller's guarded commit path; and
7. report success only from bounded controller completion and required
   post-action evidence.

Closing an SSE connection or browser tab does not cancel a backend run or prove
the outcome of a physical action. Reopen the Agent page or run journal to
inspect retained state.

FoundationPose is available to the regular Agent only for this complete
operator request:

`Use FoundationPose to establish the stationary world-to-arm-base transform.`

Generic alignment requests must not silently load it. The movement-based
replacement is still an active design; see
[Gripper-Motion Arm-Root Alignment](13_GRIPPER_MOTION_ARM_ROOT_ALIGNMENT.md).

## Stop safely

Use **Shut down Midbrain** in the portal. When the browser is unavailable, run
`Stop Midbrain.cmd` or:

```powershell
.\platform_core\scripts\stop_workspace.ps1
```

The shutdown path orders safety-critical Providers, requests their defined safe
states, and requires acknowledgements. Do not close terminal windows or kill
processes as a substitute for safe arm shutdown. Independent emergency stop
remains outside this software path.

## Status and recovery

```powershell
.\platform_core\scripts\check_status.ps1
```

| Result | Response |
|---|---|
| Manager or Fabric unavailable | Run the bounded stop path, then start the workspace once. |
| Provider `COLD` | Request it through the portal or Agent only when needed. |
| Provider live but unready | Inspect its observation page and latest structured error. |
| Observation stale | Check the producing Provider, boot identity, dependency state, and source cadence. |
| Preview created | No movement has occurred; review the exact target, scene, limits, and evidence. |
| Completion unconfirmed | Treat the action as unsuccessful and inspect measured state before another request. |
| Development page unreachable | Use component observation and logs; process liveness does not guarantee a UI. |
| Arm endpoint reachable after Manager loss | Use the documented safety path; do not force-kill load-bearing control. |

Direct development endpoints are documented by Manager and each component.
The main stable local endpoints are Manager/portal `7001`, Fabric `7002`, and
Agent UI `8000`. Treat all of them as loopback-only development interfaces.

## Forced spatial reinitialization

Reinitializing space cognition creates a new VIO epoch. It revokes alignments
and observations bound to the previous epoch and requires stationary evidence.
It is not a routine readiness check. Use the non-resetting world-readiness path
when the existing epoch is valid.

Clearing the point-cloud display removes visualization state only; it does not
reset VIO or change the world frame.

## Next references

- [Configuration and Security](07_CONFIGURATION_AND_SECURITY.md)
- [Validation](06_VALIDATION.md)
- [Current Limitations and Roadmap](09_LIMITATIONS_AND_ROADMAP.md)
- [reBot Basic safety](../providers/rebot_arm_dm/docs/SAFETY.md)
- [reBot Integrated safety](../providers/rebot_arm_integrated/docs/SAFETY.md)
