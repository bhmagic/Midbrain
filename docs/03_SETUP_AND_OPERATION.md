# Setup and Operation

## Target environment

The complete hardware path is designed for Windows 10/11 with Developer PowerShell for Visual Studio 2022.

Required tools and hardware:

- Visual Studio 2022 Build Tools with C++ workload
- Rust stable MSVC toolchain with `cargo`, `rustfmt`, and `clippy`
- Python 3.11
- CMake
- Orbbec SDK 2.8.6 development files
- Orbbec Femto Bolt

Optional FoundationPose operation additionally requires:

- NVIDIA CUDA-capable hardware and a compatible CUDA/PyTorch environment
- The upstream NVLabs FoundationPose runtime
- Git LFS for the two published checkpoint files
- SAM2 and an OpenAI API key only when using the assisted GUI initialization path

Optional reBot arm operation additionally requires:

- The supported reBot/Damiao seven-motor assembly and its reviewed local calibration
- A Windows serial connection and `motorbridge>=0.4.9`
- An Xbox-compatible controller for the current operator motion gate

The Orbbec SDK is not redistributed in this repository.

## Workspace location

The scripts support other paths, but the established workspace is:

`C:\Projects\testing_physical_ai`

Do not place API keys or device calibration in tracked source files. The setup scripts create or preserve the local `config` directory.

## First setup

Open Developer PowerShell:

```powershell
cd C:\Projects\Midbrain_git_migration
Set-ExecutionPolicy -Scope Process Bypass
.\platform_core\scripts\setup_workspace.ps1
```

The setup sequence:

1. Builds the Rust Manager and Fabric in release mode.
2. Creates `providers/orbbec_femto_bolt/.venv` and installs the Orbbec support package.
3. Builds CameraHost unless `-SkipCameraBuild` is supplied.
4. Creates `providers/local_vio/.venv` and installs the Local VIO Provider.
5. Creates a private `.venv` for each Python Skill in the core workspace.
6. Creates `test_agent/.venv` for the Test Agent and OpenAI Agents SDK.
7. Creates missing local configuration from examples without overwriting existing machine-local files.

There is no repository-root Python environment. A component may install local
editable dependencies into its own environment, but launch scripts always use
the environment owned by the process they start.

The workspace setup does not install FoundationPose or build the upstream NVLabs CUDA runtime. Set up that Provider separately after the core workspace:

```powershell
git lfs pull
.\providers\foundation_pose\scripts\setup.ps1
.\providers\foundation_pose\scripts\setup_sam2.ps1
```

`setup.ps1` creates the Provider environment, installs Midbrain integration support, seeds missing local configuration, and registers the Provider. It does not compile the complete upstream FoundationPose runtime.

Set up the two reBot arm Providers independently so each owns its own `.venv`:

```powershell
.\providers\rebot_arm_dm\scripts\setup.ps1 -WithMotorBridge
.\providers\rebot_arm_integrated\scripts\setup.ps1
.\providers\rebot_arm_dm\scripts\register.ps1
.\providers\rebot_arm_integrated\scripts\register.ps1
```

The setup commands create the two private environments and seed missing active configuration from the checked-in clean templates. The registration commands add or update the two entries in local `config\providers.json`. The repository includes factory/unverified arm templates, but it does not include an operator's active measured calibration, active controller tuning, or either virtual environment.

To set explicit SDK paths:

```powershell
.\platform_core\scripts\setup_workspace.ps1 `
  -OrbbecIncludeDir "C:\Program Files\OrbbecSDK 2.8.6\include" `
  -OrbbecLibrary "C:\Program Files\OrbbecSDK 2.8.6\lib\OrbbecSDK.lib" `
  -OrbbecBinDir "C:\Program Files\OrbbecSDK 2.8.6\bin"
```

## Start and enter Midbrain

For normal Windows operation, double-click `Start Midbrain.cmd` from the
workspace root. It starts Manager, Fabric, and the idle Agent UI service, then
opens the main Midbrain portal at `http://127.0.0.1:7001/`.

The portal is the primary interaction surface. Opening it does not activate a
Provider, execute a Skill, or authorize physical motion. Providers remain
`COLD` until an operator or approved Agent workflow requests them.

Use the PowerShell launcher for automation, recovery, or development:

```powershell
.\platform_core\scripts\run_workspace.ps1
```

Options:

- `-NoBrowser`: start without opening the portal.
- `-StartAgentUi`: start the regular and developer Agent pages on port 8000.
- `-AllowProviderAutoStart`: explicitly honor Provider `auto_start` entries.
- `-CoreOnly`: compatibility alias for Manager + Fabric only; it cannot be
  combined with `-StartAgentUi`.

`Start Midbrain.cmd` supplies `-StartAgentUi`. The direct PowerShell default
does not. Neither path honors Provider auto-start without the explicit flag.

## Operate from the main portal

Use the portal in this order:

1. Confirm the Manager and Fabric summaries are live.
2. Read Provider and Skill cards before starting anything. Process liveness,
   residency, readiness, data freshness, and active work are separate signals.
3. Open a component's read-only observation page for its manifest,
   capabilities, streams, latest state, and recent error details.
4. Enter a component development UI only through its guarded link. Acknowledge
   that administrative controls can overstep the Agent.
5. If the component is stopped, review the activation request and confirm only
   when the hardware and work area are ready.
6. Open the regular Agent for ordinary tasks. Use the developer Agent when
   wider Skill/Provider discovery is the purpose of the test.
7. Use **Shut down Midbrain** when finished. It invokes
   `platform_core\scripts\stop_workspace.ps1` through the guarded portal flow.

The portal links have distinct authority:

| Portal link | Purpose and side effects |
|---|---|
| Provider/Skill observation | Read-only state and diagnostics; does not activate or execute. |
| Development UI | Requires overstepping acknowledgement; may request bounded activation before opening. |
| Regular Agent | Curated typed Skills and approval-gated lifecycle or motion actions. |
| Developer Agent | Wider typed discovery and testing; approval requirements remain. |
| Shut down Midbrain | Runs the safety-ordered workspace stop after confirmation. |

See [Midbrain Main GUI Portal](04_MAIN_GUI_PORTAL.md) for the complete operator
workflow and recovery guidance.

## Agent interaction from the portal

Both Agent surfaces may inspect current runtime state and propose Provider
lifecycle changes. Every lifecycle change presents a plain-language
confirmation naming the Provider, requested state, and hardware consequence.

For supported relative arm motion, the Agent should:

1. Inspect current runtime state, even if an earlier conversation reported the
   controllers running.
2. Request approval for Basic to reach `HOT`.
3. Request approval for Integrated to reach `HOT`.
4. Produce a nonphysical IK preview from the latest measured pose.
5. Present a separate approval for execution of that exact preview.
6. Wait for the bounded controller result and report success only when physical
   completion is confirmed.

Repeated relative commands are cumulative from the latest measured pose. The
Agent does not convert them into absolute world-coordinate requests. Safe-home
is a separate approval-gated Basic Controller operation.

Both Agent pages support per-run Agent model, reasoning-effort, and configured
visual-backend selection. Terra with medium reasoning is the balanced default.
A stronger model can improve interpretation but cannot replace controller
validation, approval, fencing, collision checks, or physical safety controls.

## Direct endpoints for development and recovery

Use these when the portal is unavailable or when developing a component:

| Service | URL |
|---|---|
| Midbrain main portal | `http://127.0.0.1:7001/` |
| Manager health/control | `http://127.0.0.1:7001/v1` |
| Fabric health/state | `http://127.0.0.1:7002` |
| Camera Provider control | `http://127.0.0.1:7101` |
| Local VIO Provider control | `http://127.0.0.1:7102` |
| FoundationPose Provider control | `http://127.0.0.1:7103` |
| reBot Arm DM Basic control | `http://127.0.0.1:8791` |
| reBot Arm Integrated control/GUI | `http://127.0.0.1:8793` |
| Regular Agent UI | `http://127.0.0.1:8000/` |
| Developer Agent UI | `http://127.0.0.1:8000/dev` |
| Calibration GUI | `http://127.0.0.1:8111` |

## Status and stop

```powershell
.\platform_core\scripts\check_status.ps1
.\platform_core\scripts\stop_workspace.ps1
```

Runtime logs are written under `platform_core\logs` and are intentionally ignored by Git.

## reBot arm discovery and test operation

When Integrated is HOT and ready, Manager `GET /v1/capabilities` advertises usable MIT one-shot/continuous and limited POS_VEL one-shot. Provider `GET http://127.0.0.1:8793/v1/capabilities` maps the discoverable capabilities and GUI operations to their HTTP or Fabric invocation.

POS_VEL one-shot is labeled limited to paths at or below 20 cm with no payload or high external load. POS_VEL continuous and arm POS_TOR one-shot remain experimental/unstable GUI tests and are intentionally absent from Manager capability discovery.

The current upstream flow is target/settings staging through Fabric stream `robot_arm.primary.integrated.command`, followed by the local operator's Engage + Xbox LB release. Use the provider's documented `stop_physical_gui_test.ps1` path for authoritative safe-home termination.

## Forced VIO reinitialization

Use **Force reinitialize origin** in the Test Agent while the camera is stable. A reset creates a new Local VIO session epoch and world frame. The viewer suspends new insertion, switches epoch, clears old-epoch points, reopens shared-memory readers, and resumes capture when the new pose and RGB-D data are available.

Expected progression:

`SUSPENDED_FOR_REINITIALIZATION → WAITING_FOR_NEW_SESSION_FRAME → CAPTURING`

## Clear visualization only

Use **Clear point cloud** to remove accumulated display points without resetting VIO or changing the current coordinate epoch.

## FoundationPose operator workflow

Start the core workspace, camera Provider, and FoundationPose tracking GUI:

```powershell
.\providers\foundation_pose\scripts\run_tracking_gui.ps1
```

Keep the arm still during initialization. Freeze a suitable RGB-D frame, request and review the Base and Gripper boxes and positive points, generate the cropped SAM2 masks, inspect the refined results, and submit tracking only when both masks cover the intended rigid surfaces without unrelated geometry.

The tested Base refinement uses median Lab color distance 30 followed by radius-2 dilation. The tested neon-green Gripper-root refinement uses a median RGB seed with 10% per-channel drift followed by radius-2 dilation. These are empirical defaults, not universal segmentation guarantees.

Base tracking is selectable up to 10 Hz. The experimental Gripper selector exposes rates up to 60 Hz, but actual throughput remains bounded by inference and hardware load; raising the requested rate did not correct the observed Gripper stability problem. Use the lowest stable rate that supplies timely measurements.
