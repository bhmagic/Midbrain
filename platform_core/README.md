# Midbrain Platform Core

Platform Core contains the Resource Provider Manager, World State Fabric, and
Windows workspace lifecycle scripts. It is replaceable infrastructure, not a
robot- or Agent-specific application.

## Responsibilities

Manager owns:

- Provider discovery, process lifecycle, dependency activation, and health;
- per-capability readiness and selection surfaces;
- resource, motion-inhibit, authority, and shutdown coordination;
- guarded activation, flat translation-only refinement, and revocation of
  reviewed workcell calibration; and
- the system portal and implementation-neutral observation pages.

Fabric owns:

- timestamped observation and stream metadata;
- passive synchronization and temporal lookup;
- Provider, boot, sequence, calibration, and frame provenance;
- the native timestamped transform graph; and
- references to large payloads that remain in transport-specific storage.

Neither service replaces a hardware Provider's final command validation or
independent emergency stop.

## Setup

From Developer PowerShell in the repository root:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\platform_core\scripts\setup_workspace.ps1
```

The setup creates or preserves machine-local configuration under `config` and
builds Manager and Fabric with the supported Windows toolchain. It does not
store active configuration inside `platform_core`.

## Operate

Normal desktop operation uses `Start Midbrain.cmd` and the Manager portal at
`http://127.0.0.1:7001/`.

Direct development and recovery commands are:

```powershell
.\platform_core\scripts\run_workspace.ps1
.\platform_core\scripts\check_status.ps1
.\platform_core\scripts\stop_workspace.ps1
```

The launcher uses bounded health gates, refuses occupied requested ports, and
does not replace a running workspace. Provider auto-start requires the explicit
`-AllowProviderAutoStart` option; unattended arm auto-start remains refused.

The shutdown path fences new work, orders motion Providers, requires the Basic
arm safe-state acknowledgement when applicable, stops ordinary Providers, and
leaves core-process termination to the workspace supervisor. A
safety-critical Provider may disable automatic force-kill after its graceful
timeout so powered support is not removed by process policy.

See [Setup and Operation](../docs/03_SETUP_AND_OPERATION.md) for the operator
workflow.

## Local interfaces

- Manager and portal: `http://127.0.0.1:7001/`
- Manager API: `http://127.0.0.1:7001/v1`
- Fabric API: `http://127.0.0.1:7002/`

These are loopback development interfaces. The detailed current endpoint
inventory is in [Local HTTP API](docs/http_api.md). Semantic compatibility is
defined by contracts and schemas, not by assuming these prototype URLs are a
permanent remote protocol.

## Provider lifecycle

- `COLD`: no Provider process exists.
- `WARM`: the process exists but releases protected control handles, high-rate
  work, and most optional resources.
- `HOT`: required initialization is complete and declared capabilities may be
  ready.

Process liveness, residency, health, and per-capability readiness are separate
signals. A `HOT` Provider can still have a degraded optional capability.
Manager resolves declared Provider dependencies before the requested Provider
enters `HOT`.

## Authority and observations

Manager-level authority identifies the upstream owner and scope. Physical
Providers retain independent fenced leases and final enforcement. The two
fencing namespaces must not be compared numerically or collapsed into a single
unverified token.

Fabric is passive with respect to consumer freshness policy. It reports source
and arrival times, identity, sequence, validity, and structural expiry. The
consuming Skill decides whether that evidence is suitable for its operation.

## Validation

Run the workspace validation entry point:

```powershell
.\scripts\validate.ps1
```

Platform Core's Rust suite and current validation boundary are summarized in
[VALIDATION.md](VALIDATION.md). Guarded hardware acceptance remains separate
from stopped core tests.

## Contracts

Start with the [Provider Contract](../contracts/01_resource_provider_contract.md),
[Fabric Transport Specification](../contracts/03_world_state_fabric_transport_specification.md),
[Safety and Lease Policy](../contracts/05_safety_and_lease_policy.md), and
[Timestamped Transform Graph](../contracts/06_timestamped_transform_graph.md).
