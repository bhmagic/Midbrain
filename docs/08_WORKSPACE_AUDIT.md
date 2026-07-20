# Workspace Audit

## Compared inputs

The cleanup compared:

- the documented `physical_ai_space_cognition_handover_complete_v0_3_10` source workspace; and
- the supplied `testing_physical_ai` working workspace.

All 163 files in the documented source workspace were present in the working workspace and were byte-for-byte identical. No undocumented edit to the documented RGB-D platform source was found.

## Undocumented additions found

The working workspace contained additional material not represented in the v0.3.10 source manifest:

| Category | Examples | Cleanup decision |
|---|---|---|
| Rust build output | `platform_core/target` | Excluded. Rebuild from source. |
| Native CameraHost build and SDK runtime | `native_host/build`, `.exe`, `.dll`, extension binaries | Excluded. Proprietary/platform-specific output must be rebuilt from an installed SDK. |
| Python generated output | `build`, `*.egg-info`, `__pycache__`, `.pytest_cache` | Excluded. |
| Virtual environments | `.venv` trees | Excluded. |
| Runtime state | logs, PID JSON, captures | Excluded. |
| Machine-local configuration | `config/providers.json`, `config/system.env` | Excluded; examples retained. |
| Device-specific calibration | serial-bound Orbbec calibration and backup | Excluded to protect device identity and local measurements. |
| Opaque archive | `config/config.rar` | Excluded because its contents and provenance were not documented. |
| Unrelated providers | `rebot_arm_dm`, `rebot_arm_integrated`, backups and environments | Excluded because the requested GitHub scope is RGB-D camera plus brand-neutral pose. |
| Rust lockfile | `platform_core/Cargo.lock` | Retained for reproducible application builds. |

## Source scope retained

- Manager and Fabric main frame
- framework contracts
- Orbbec Femto Bolt RGB-D/IMU Provider
- brand-neutral Local VIO pose Provider
- Test Agent point-cloud/pose GUI
- accelerometer calibration GUI
- package scripts, tests, and source documentation

## Documentation corrections

The package version files indicated Manager/Fabric `0.3.0`, camera Provider `0.3.1`, Local VIO `0.2.2`, and Test Agent `0.2.9`. Some older README release wording still referred to `0.2.0`; the GitHub-facing documentation and package README headings were corrected to the current component versions.
