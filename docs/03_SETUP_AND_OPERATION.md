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

## Forced VIO reinitialization

Use **Force reinitialize origin** in the Test Agent while the camera is stable. A reset creates a new Local VIO session epoch and world frame. The viewer suspends new insertion, switches epoch, clears old-epoch points, reopens shared-memory readers, and resumes capture when the new pose and RGB-D data are available.

Expected progression:

`SUSPENDED_FOR_REINITIALIZATION → WAITING_FOR_NEW_SESSION_FRAME → CAPTURING`

## Clear visualization only

Use **Clear point cloud** to remove accumulated display points without resetting VIO or changing the current coordinate epoch.
