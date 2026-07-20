# Midbrain Physical AI RGB-D Platform

A framework-neutral physical-agent platform for RGB-D sensing and local space cognition. The repository contains a Rust control/state core, an Orbbec Femto Bolt RGB-D provider, a brand-neutral camera-plus-IMU pose provider, and two local web mockups used as tutorials and functional checks.

## Included components

| Component | Path | Purpose |
|---|---|---|
| Resource Provider Manager | `platform_core/manager` | Provider process lifecycle, health, capability discovery, request forwarding, and motion-inhibit coordination. |
| World State Fabric | `platform_core/fabric` | Timestamped observation storage, stream discovery, synchronized lookup, and transform graph. |
| Contracts | `contracts` | Framework-neutral Provider, Fabric, Skill, calibration, VIO, and safety contracts. |
| Orbbec Femto Bolt Provider | `providers/orbbec_femto_bolt` | Brand-specific RGB, depth, IR, point cloud, IMU, calibration, identity, and static transforms. |
| Local VIO Provider | `providers/local_vio` | Brand-neutral inertial-first RGB-D/IMU pose estimation and dynamic body transforms. |
| Test Agent and Point-Cloud GUI | `test_agent` | Mock agent, initialization skill, live point-cloud viewer, pose display, and estimator diagnostics. |
| IMU Calibration GUI | `providers/orbbec_femto_bolt/python/orbbec_femto_provider/calibration_web` | Six-position accelerometer calibration workflow for the camera IMU. |

## Architecture

The Manager is the control plane. The Fabric is the timestamped state plane. Providers remain persistent and own hardware or long-lived computation. Skills perform bounded work by requesting Provider capabilities and consuming Fabric observations.

The camera Provider publishes large RGB-D payloads through Windows named shared memory and publishes generation-checked references through the Fabric. The Local VIO Provider consumes ordered IMU history and RGB-D observations, propagates pose continuously from the IMU, and uses RGB-D or synchronized IR/depth as correction measurements.

See [the documentation index](docs/README.md) and [the architecture guide](docs/01_ARCHITECTURE_AND_DATA_FLOW.md).

## Requirements

The complete hardware path targets Windows 10/11 with:

- Visual Studio 2022 Build Tools and Developer PowerShell
- Rust stable MSVC toolchain
- Python 3.11
- CMake
- Orbbec SDK 2.8.6
- Orbbec Femto Bolt

The Orbbec SDK and runtime binaries are not included in this repository.

## Setup

From Developer PowerShell:

```powershell
cd C:\Projects
Expand-Archive .\physical_ai_platform_rgbd_2026_07_20.zip -DestinationPath .
Rename-Item .\physical_ai_platform_rgbd_2026_07_20 testing_physical_ai
cd .\testing_physical_ai
Set-ExecutionPolicy -Scope Process Bypass
.\platform_core\scripts\setup_workspace.ps1
.\platform_core\scripts\run_workspace.ps1
```

Default local endpoints:

| Service | URL |
|---|---|
| Manager | `http://127.0.0.1:7001` |
| Fabric | `http://127.0.0.1:7002` |
| Test Agent GUI | `http://127.0.0.1:8000` |
| IMU Calibration GUI | `http://127.0.0.1:8111` |

## Two functional tutorials

1. [Point cloud and pose tutorial](docs/04_TUTORIAL_POINT_CLOUD_AND_POSE.md): starts the mock agent, initializes local space cognition, displays the live world-frame point cloud, and checks pose/reset behavior.
2. [IMU calibration tutorial](docs/05_TUTORIAL_IMU_CALIBRATION.md): captures six camera orientations, solves accelerometer scale/offset, writes a device-bound calibration, and reloads the Provider.

These are operational checks, not production qualification.

## Validation

Run the source validation script from PowerShell:

```powershell
.\scripts\validate.ps1
```

The cleaned source snapshot passes all 37 Python regression tests. Rust and the native CameraHost must be compiled on a machine with the required toolchains; the native CameraHost additionally requires the Orbbec SDK.

## Repository hygiene

Machine-local configuration, API keys, device serial numbers, calibration data, SDK binaries, build outputs, virtual environments, logs, captures, PID files, and unrelated robot-arm packages were intentionally excluded. The audit is recorded in [docs/08_WORKSPACE_AUDIT.md](docs/08_WORKSPACE_AUDIT.md).

## Publishing

After validation, use:

```powershell
.\scripts\publish_github.ps1 -RepositoryUrl "https://github.com/bhmagic/Midbrain.git"
```

The script never stores a token. Authentication is handled by Git Credential Manager or the GitHub CLI. See [docs/10_RELEASE_AND_GITHUB.md](docs/10_RELEASE_AND_GITHUB.md).

## Release status

Integrated source baseline:

- Manager/Fabric `0.3.0`
- Orbbec Femto Bolt Provider `0.3.1`
- Local VIO Provider `0.2.2`
- Test Agent `0.2.9`
- Contracts working draft `0.3.8`

Formal trajectory accuracy, deterministic replay, camera/IMU time-offset estimation, long visual-outage drift, and production-backend comparison remain open.

## License

Original project code is released under the permissive [MIT License](LICENSE). Third-party packages, SDKs, drivers, assets, and any externally derived code remain subject to their original terms. The external-code and dependency-license audit is still pending; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
