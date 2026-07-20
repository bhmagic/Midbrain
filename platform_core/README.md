# Physical AI Platform Core 0.3.0

This replaceable package contains the Resource Provider Manager, World State Fabric, and Windows workspace scripts.

Version 0.3.0 is the current integrated source baseline. Build and exercise the Rust services on the target Windows machine before treating a new checkout as hardware verified.

## Install or replace

Extract into `C:\Projects\testing_physical_ai`, producing `platform_core`.

Before replacing:

```powershell
.\platform_core\scripts\stop_workspace.ps1
Remove-Item .\platform_core -Recurse -Force
```

Keep `config`, `.venv`, `test_agent`, and `providers`.

## Setup

Use Developer PowerShell for Visual Studio 2022:

```powershell
cd C:\Projects\testing_physical_ai
Set-ExecutionPolicy -Scope Process Bypass
.\platform_core\scripts\setup_workspace.ps1
```

## Operate

```powershell
.\platform_core\scripts\run_workspace.ps1
.\platform_core\scripts\check_status.ps1
.\platform_core\scripts\stop_workspace.ps1
```

## New v0.2 interfaces

- Manager capability catalog: `http://127.0.0.1:7001/v1/capabilities`
- Fabric stream catalog: `http://127.0.0.1:7002/v1/streams`
- Fabric timestamp-nearest bundles: `http://127.0.0.1:7002/v1/sync`

The core does not contain or overwrite `config`.

## v0.3 space-cognition infrastructure

The Fabric now exposes `/v1/schemas`, `/v1/transforms`, and timestamped `/v1/transform` composition. The Manager now forwards provider-specific requests and coordinates whole-robot motion-inhibit leases. The supplied provider configuration includes the Femto Bolt and Local VIO providers.

Run `scripts\validate.ps1` from the repository root and complete the target Windows validation before treating a checkout as hardware verified.
