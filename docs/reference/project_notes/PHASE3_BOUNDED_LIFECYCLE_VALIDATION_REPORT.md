# Phase 3 Bounded Workspace Lifecycle Validation Report

Date: 2026-07-27

## Scope

This regression validates unattended core startup and shutdown without starting
either arm provider or submitting any physical action. The Orbbec camera
provider remained the only configured auto-start provider.

## Corrected failure mode

The former `run_workspace.ps1` performed an implicit `stop_workspace.ps1`
before startup. When invoked through another PowerShell host, that unbounded
pre-start path could leave the parent waiting indefinitely.

`run_workspace_bounded.ps1` now:

- refuses configurations that auto-start an arm provider;
- refuses occupied requested ports instead of replacing a live workspace;
- starts Fabric, Manager, and optional Test Agent UI as independent processes;
- gives each HTTP health gate an internal deadline;
- writes the PID file only after every requested service is healthy;
- gives non-arm auto-start providers a bounded cleanup request after a failed
  health gate;
- stops only processes created by the failed invocation; and
- never invokes the old implicit pre-start shutdown.

`run_workspace.ps1` delegates to this bounded implementation.

## Evidence

The direct core-only bounded entrypoint reached healthy Fabric and Manager in
1.651 seconds. The operator-facing `run_workspace.ps1` entrypoint then reached
the same state in 1.467 seconds.

A second start while Fabric was already listening refused the operation in
0.117 seconds. It did not stop or disturb the healthy Fabric and Manager
instances.

The default `stop_workspace.ps1` path detected that Manager shutdown execution
was enabled, completed the Manager-owned provider sequence, and stopped the
supervisor-owned core PIDs in 2.914 seconds. A final TCP and PID audit found:

- ports 7001, 7002, 7101, 7102, 7103, 8000, 8791, and 8793 closed;
- Fabric, Manager, and camera test PIDs exited; and
- `platform_core/run/pids.json` removed.

All four lifecycle PowerShell files passed parser validation. No arm provider
was started, and no safe-home, gripper, lease, mode, or motion request occurred.

## Supported invocation

Use the current PowerShell host instead of starting another nested host:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
& '.\platform_core\scripts\run_workspace.ps1' -CoreOnly -NoBrowser
```

The process-scope execution-policy change is temporary and applies only to that
PowerShell process.
