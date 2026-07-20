# Guide to the Space Cognition v0.3.10 Source Packages

## Workspace folders

| Component | Extracted folder |
|---|---|
| Manager and Fabric v0.3.0 | `C:\Projects\testing_physical_ai\platform_core` |
| Femto Bolt Provider v0.3.1 | `C:\Projects\testing_physical_ai\providers\orbbec_femto_bolt` |
| Local VIO Provider v0.2.2 | `C:\Projects\testing_physical_ai\providers\local_vio` |
| Test Agent v0.2.9 | `C:\Projects\testing_physical_ai\test_agent` |
| Contracts | `C:\Projects\testing_physical_ai\contracts` |
| Project documentation | `C:\Projects\testing_physical_ai\project_docs` |

Persistent configuration remains in `C:\Projects\testing_physical_ai\config` and should not be replaced during source-overlay installation.

## Install or upgrade

Use Developer PowerShell for Visual Studio 2022:

```powershell
cd C:\Projects\testing_physical_ai
Set-ExecutionPolicy -Scope Process Bypass
.\platform_core\scripts\stop_workspace.ps1
.\platform_core\scripts\setup_workspace.ps1
```

## Start

```powershell
.\platform_core\scripts\run_workspace.ps1
```

Endpoints:

- Manager: `http://127.0.0.1:7001`
- Fabric: `http://127.0.0.1:7002`
- Test UI: `http://127.0.0.1:8000`

## Expected VIO configuration

The Local VIO Provider entry should use:

```text
--backend inertial_first_rgbd_eskf
--inertial-publish-hz 100
--ir-enabled
--ir-sync-tolerance-us 8000
--gravity-gyro-limit-radps 0.012
--feature-preprocess-mode adaptive_circular_lcn
```

## Initial hardware checks

1. Leave the camera stationary while startup initialization runs.
2. Confirm the Skill reaches `SUCCEEDED`.
3. Confirm VIO reports `IMU_PROPAGATION_FIRST_VISUAL_KEYFRAME`, followed by inertial propagation and RGB-D corrections.
4. Confirm the visual source is normally `RGBD`.
5. In dim conditions, observe whether `IR_DEPTH` is selected only when its correction is stronger.
6. Confirm gravity remains `OFF`, `READY`, or `ACTIVE` with the previously tuned behavior.
7. Confirm the orthographic point cloud remains spatially stable during panning.

## Stop

```powershell
.\platform_core\scripts\stop_workspace.ps1
```
