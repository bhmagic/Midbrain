# reBot Arm Basic Controller: Windows Setup and Bring-Up

This procedure applies to the hardware-facing Basic Provider. Complete stopped
software validation and simulation before opening a real motor connection.

## Prerequisites

- Windows 10/11
- Developer PowerShell for Visual Studio 2022
- Python 3.11
- Git and the Rust toolchain (`cargo` and `rustc`)
- network access for the reviewed MotorBridge source and Rust dependencies
- supported reBot B601-DM hardware and reviewed local calibration
- exclusive access to the arm's serial interface
- independent emergency stop and a cleared, padded work area

Run commands from the Midbrain repository root. The package must remain at
`providers/rebot_arm_dm`; no fixed absolute workspace path is required.

## Create the component environment

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\providers\rebot_arm_dm\scripts\setup.ps1 -WithMotorBridge
```

The setup creates the Provider-owned `.venv`, checks out the reviewed
MotorBridge commit under ignored `.artifact_work`, applies the tracked additive
state-generation/receive-age patch, compiles its native ABI and gateway,
installs that build, and seeds missing machine-local model and calibration files
from sanitized templates. It preserves existing active configuration.

Register the Provider with Manager:

```powershell
.\providers\rebot_arm_dm\scripts\register.ps1
```

Registration merges the Provider entry into ignored local
`config/providers.json` without replacing unrelated components.

## Stopped validation

```powershell
.\providers\rebot_arm_dm\scripts\verify.ps1
```

This suite must not move the arm. Resolve Python, package, configuration, and
native dependency failures before continuing.

## Simulation first

Run the package's documented simulation or fake-transport path and verify:

- state and feedback publication;
- lease acquisition, renewal, fencing, and expiry;
- MIT and endpoint command validation;
- safe-home planning without hardware submission;
- gravity-float and powered-safe-state transitions;
- serial timeout and uncertain-outcome reporting; and
- graceful stop without orphaned control loops.

Do not treat simulation as physical calibration or hardware qualification.

## Identify the serial device

Disconnect other Damiao/MotorBridge clients. Determine the installed arm's
actual Windows COM port and record it only in machine-local configuration.
Never publish a serial number or active calibration accidentally.

If the port is busy, stop the existing workspace and vendor tools before
retrying. Do not run two Basic Providers against one motor bus.

## Read-only hardware start

The first physical connection must not request safe-home, mode changes, or
motion. Start Basic through its documented read-only or non-commanding bring-up
path and confirm:

- the expected seven motor identities are present;
- joint feedback is plausible and ordered correctly;
- configured motor models and protocol revisions match the hardware;
- no command was submitted;
- no motor reports a fault; and
- the Provider can relinquish and stop cleanly.

Stop immediately on an unexpected identity, direction, joint value, motor
mode, communication fault, or command counter.

## Calibration configuration and development UI boundary

Factory model data is not measured installation calibration. Basic consumes a
reviewed machine-local calibration profile but does not generate one. The
Hardware Development UI provides attended manual joint testing only; it does
not estimate or write calibration parameters.

See:

- [Hardware Development UI](DEVELOPMENT_UI.md)
- [Safety behavior](SAFETY.md)

Provision and review calibration configuration separately before allowing
ordinary Provider startup to consume it.

## Manager and Fabric operation

Start Midbrain normally, then request Basic through the portal or a guarded
Agent workflow. Check:

- Manager instance and Provider boot identity;
- `COLD`, `WARM`, or `HOT` residency;
- health and per-capability readiness;
- measured arm state and command counters;
- current lease owner and fencing generation; and
- latest structured error or safe-state reason.

Basic is the sole owner of the motor transport. Integrated and other clients
must command it through the fenced Provider contract rather than opening the
serial device directly.

## Normal shutdown

Use the Midbrain portal or:

```powershell
.\platform_core\scripts\stop_workspace.ps1
```

The safety-ordered path stops Integrated before Basic and requires the Basic
safe-state acknowledgement. If Manager is unavailable while the Basic endpoint
still provides powered support, the workspace script refuses an automatic
force-kill. Follow the documented safety recovery path; do not terminate the
process merely to free the port.

## Common failures

### COM port busy

Another process owns the serial device. Stop it cleanly. Do not change ports or
start a second controller until the physical device identity is known.

### MotorBridge unavailable

Re-run component setup with `-WithMotorBridge`. Basic intentionally rejects a
MotorBridge build that cannot prove each feedback generation is newer than the
current control-cycle request. Do not copy unknown native binaries into the
repository or commit them.

### Wrong Python environment

Use `providers/rebot_arm_dm/.venv`. The Basic and Integrated Providers do not
share environments.

### Provider health unavailable

Inspect the Provider process output, Manager observation page, active local
configuration, COM ownership, and native dependency loading. Repeated `HOT`
requests do not repair a deterministic startup fault. For a runtime control
fault, one Manager `HOT` transition may requalify Basic only after recent
generation-verified feedback is available; it fences the previous lease and
returns to gravity float without replaying motion.

### PowerShell script blocked

Use the process-scoped policy for the current trusted Developer PowerShell
session:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
```

Do not weaken the machine-wide policy as a routine setup step.
