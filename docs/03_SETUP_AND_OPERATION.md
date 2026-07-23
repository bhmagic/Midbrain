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
cd C:\Projects\testing_physical_ai
Set-ExecutionPolicy -Scope Process Bypass
.\platform_core\scripts\setup_workspace.ps1
```

The setup sequence:

1. Builds the Rust Manager and Fabric in release mode.
2. Creates a shared Python 3.11 virtual environment.
3. Installs the Orbbec support package.
4. Builds CameraHost unless `-SkipCameraBuild` is supplied.
5. Installs the Local VIO Provider.
6. Installs the Test Agent.
7. Creates missing local configuration from examples without overwriting existing machine-local files.

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

The setup commands create the two private environments and seed missing local configuration. The registration commands add or update the two entries in local `config\providers.json`. The repository does not include machine-local arm calibration, runtime configuration, or either virtual environment.

To set explicit SDK paths:

```powershell
.\platform_core\scripts\setup_workspace.ps1 `
  -OrbbecIncludeDir "C:\Program Files\OrbbecSDK 2.8.6\include" `
  -OrbbecLibrary "C:\Program Files\OrbbecSDK 2.8.6\lib\OrbbecSDK.lib" `
  -OrbbecBinDir "C:\Program Files\OrbbecSDK 2.8.6\bin"
```

## Start

```powershell
.\platform_core\scripts\run_workspace.ps1
```

Options:

- `-NoBrowser`: do not open the Test Agent UI automatically.
- `-CoreOnly`: start Manager and Fabric without Python Providers or the GUI.

Default endpoints:

| Service | URL |
|---|---|
| Manager health/control | `http://127.0.0.1:7001` |
| Fabric health/state | `http://127.0.0.1:7002` |
| Camera Provider control | `http://127.0.0.1:7101` |
| Local VIO Provider control | `http://127.0.0.1:7102` |
| FoundationPose Provider control | `http://127.0.0.1:7103` |
| reBot Arm DM Basic control | `http://127.0.0.1:8791` |
| reBot Arm Integrated control/GUI | `http://127.0.0.1:8793` |
| Test Agent GUI | `http://127.0.0.1:8000` |
| Calibration GUI | `http://127.0.0.1:8111` |

## Normal startup sequence

1. Secure the camera and leave it still.
2. Start the workspace.
3. Confirm the camera Provider becomes `HOT` and RGB/depth observations appear.
4. The Test Agent starts Initialize Space Cognition automatically unless disabled.
5. The Skill acquires motion inhibit and waits for the Local VIO Provider to observe it.
6. The Local VIO initializer selects recent accelerometer and gyro windows in a common IMU time domain.
7. Confirm the selected initialization counts reach the configured requirement, normally `80/80`.
8. Confirm the initialization blocker becomes `none` and the Skill reaches `SUCCEEDED`.
9. Confirm inertial propagation steps increase and `localization.body.pose` is available.
10. Confirm the point-cloud GUI changes to `CAPTURING`.

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
