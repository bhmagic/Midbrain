# Contributing

## Scope

Changes should preserve the separation between:

- Manager control plane
- Fabric timestamped state plane
- persistent Resource Providers
- finite Skills
- large payload transport outside the Fabric

Do not move camera payload bytes into normal Fabric JSON observations. Do not make the Local VIO Provider camera-brand-specific.

## Development checks

Before opening a change:

```powershell
.\scripts\validate.ps1
```

For camera-native changes, also build and verify CameraHost on Windows with the Orbbec SDK installed.

## Python environment isolation

Every Python Skill, Provider, and Agent owns `.venv` inside its component
folder. Do not create or reference a repository-root `.venv`, and do not launch
one component with another component's interpreter. When a process needs local
Python libraries from another component, declare or install those dependencies
inside the process owner's environment. Setup, run, check, registration, and
documentation changes must preserve this boundary.

## Configuration and data

Never commit:

- API keys or access tokens
- `config/system.env` or `config/api_keys.env`
- device serial numbers or serial-bound calibration
- captures, point clouds, logs, PID files, or crash dumps
- generated Skill plans, failure captures, runtime audit cursors, or replay payloads
- virtual environments, package build directories, Rust `target`, or CMake output
- external FoundationPose or SAM2 source checkouts
- Orbbec SDK binaries or other third-party proprietary runtime files

## Contracts

Provider or Fabric interface changes should update the relevant document under `contracts` and include a compatibility note. Prefer additive schemas and capability discovery over hidden provider-specific assumptions.

## Safety

This repository is perception and state infrastructure. Do not enable physical robot motion without separate reviewed control-authority leases, safe relinquish behavior, independent emergency stop, and hardware-specific validation.

## Licensing and external code

Contributions to original project code are accepted under the repository's MIT License. Do not add copied, adapted, generated, or vendored external code unless its source, license, attribution, and redistribution requirements are recorded. Update `THIRD_PARTY_NOTICES.md` and include any required license text before merging such material.
