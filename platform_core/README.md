# Physical AI Platform Core 0.3.0

This replaceable package contains the Resource Provider Manager, World State Fabric, and Windows workspace scripts.

Version 0.3.0 contains the native transform graph, provider lifecycle,
capability/authority migration surfaces, enforced reviewed-workcell
activation, and Manager-owned shutdown sequence.

## Install or replace

Extract into `C:\Projects\testing_physical_ai`, producing `platform_core`.

Before replacing:

```powershell
.\platform_core\scripts\stop_workspace.ps1
Remove-Item .\platform_core -Recurse -Force
```

Keep `config`, `test_agent`, `providers`, and `skills`. Python environments are
component-local `.venv` directories and can be recreated by their setup
scripts.

## Setup

Use Developer PowerShell for Visual Studio 2022:

```powershell
cd C:\Projects\Midbrain_git_migration
Set-ExecutionPolicy -Scope Process Bypass
.\platform_core\scripts\setup_workspace.ps1
```

## Operate

```powershell
.\platform_core\scripts\run_workspace.ps1
.\platform_core\scripts\check_status.ps1
.\platform_core\scripts\stop_workspace.ps1
```

## Provider stop escalation

Provider entries may set `force_kill_on_stop_timeout`. The default is `true`
for backward compatibility: after `graceful_stop_timeout_ms`, Manager
terminates a provider process that has not exited. Set it to `false` for a
provider whose process supplies safety-critical powered support. In that mode,
Manager reports the timeout without terminating the process; only the explicit
`/v1/providers/{id}/kill` operation bypasses the policy.

## New v0.2 interfaces

- Manager capability catalog: `http://127.0.0.1:7001/v1/capabilities`
- Manager advisory capability binding: `POST http://127.0.0.1:7001/v1/capability-bindings`
- Manager advisory control authority: `POST http://127.0.0.1:7001/v1/control-authority/leases`
- Manager shutdown dry run: `POST http://127.0.0.1:7001/v1/shutdown/plan`
- Manager gated shutdown execution: `POST http://127.0.0.1:7001/v1/shutdown/{id}/execute`
- Manager workcell activation: `POST http://127.0.0.1:7001/v1/workcell-calibrations/activate`
- Manager workcell revocation: `POST http://127.0.0.1:7001/v1/workcell-calibrations/{id}/revoke`
- Fabric stream catalog: `http://127.0.0.1:7002/v1/streams`
- Fabric timestamp-nearest bundles: `http://127.0.0.1:7002/v1/sync`

The core does not contain or overwrite `config`.

## v0.3 space-cognition infrastructure

The Fabric now exposes `/v1/schemas`, `/v1/transforms`, and timestamped `/v1/transform` composition. The Manager now forwards provider-specific requests and coordinates whole-robot motion-inhibit leases. The supplied provider configuration includes the Femto Bolt and Local VIO providers.

Manager is the enforcement boundary that turns an exact reviewed stationary
alignment candidate into a bounded motion-usable workcell activation. It
checks current camera and VIO identity before publication, permits only one
active calibration, and publishes explicit revocation so Fabric suppresses the
older static edge.

The current Manager release binary has been rebuilt on the target Windows
machine. Manager-owned shutdown execution passed its guarded hardware
validation. It is enabled in this workspace and remains disabled in the
configuration template for new workspaces.

Capability bindings are currently advisory. Existing explicit
`/v1/providers/{id}/request` clients remain valid, and a binding request may
declare explicit provider IDs as fallbacks while selection behavior is being
measured. Reading a stored binding revalidates its provider instance, boot,
readiness, health, and residency and reports stale or fallback state without
silently changing physical authority.

A cold explicit fallback is reported as
`FALLBACK_REQUIRES_ACTIVATION`; binding never starts it. If that same provider
is independently activated and advertises the capability, a new binding may
return it as a `CURRENT` `AVAILABLE_CAPABILITY` selection. Enforcing consumers
must reject the cold state and revalidate the selected instance and boot.

Control-authority leases are also advisory in Phase 1. They preserve ownership
history, expiry, preemption state, and fencing generations while providers
compare the Manager view with their existing local authority. They do not yet
authorize or reject physical commands.

The shutdown-plan route remains non-mutating. A distinct execution route is
available only when `MANAGER_SHUTDOWN_EXECUTION_ENABLED=true` and the caller
supplies the exact confirmation token. It fences new work, stops motion
providers, requires Basic safe-state acknowledgement, stops ordinary providers,
and stops Basic last among providers. Fabric and Manager are deliberately left
for the workspace supervisor.

The current workspace enables this validated route, so
`stop_workspace.ps1` uses it by default with a hard polling deadline. Use
`-UseLocalShutdownFallback` for the retained local sequence.
`-UseManagerShutdownExecution` remains as an explicit force/check option for
workspaces whose configuration may still disable the route.

## Bounded automation startup

The original launcher previously performed an unbounded pre-start shutdown.
On this Windows host, a nested PowerShell parent could therefore remain waiting
even after detached children were healthy. `run_workspace.ps1` now delegates to
the bounded lifecycle implementation and never performs that implicit shutdown.
An occupied core port is reported immediately instead of replacing a running
workspace.

Use the bounded automation entrypoint directly in the existing PowerShell
session:

```powershell
.\platform_core\scripts\run_workspace_bounded.ps1 -CoreOnly -NoBrowser
```

Both launcher names now refuse occupied core ports and unattended arm
auto-start configurations, create independent core processes, apply a deadline
to every health gate, and record PIDs only after all requested services are
healthy. They do not call the legacy pre-start shutdown path. A failed health
gate first gives configured non-arm auto-start providers a bounded stop request
and then stops the core processes created by that launcher invocation.
