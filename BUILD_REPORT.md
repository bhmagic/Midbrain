# Build and Validation Report

Date: 2026-07-26

## Clean-configuration completeness audit - 2026-07-26

### Scope and corrections

- Traced active configuration reads, defaults, directory creation, and setup/registration behavior across Manager, Fabric, Test Agent, camera, Local VIO, FoundationPose, Basic arm, Integrated arm, and Stationary World-Space Arm Finder.
- Added root recovery examples for `system.env` and `providers.json`. The non-interactive core initializer now also creates a blank `api_keys.env`; all three active files are preserved when already present.
- Synchronized root, platform-core, and Test Agent fallbacks. Canonical Provider entries now inherit Manager/Fabric URLs and the camera shared-memory mapping name from `system.env`.
- Corrected FoundationPose repair seeding to reproduce the canonical license/provenance names and reference atlases instead of its older partial/legacy layout.
- Added `config/BASELINE_INVENTORY.md` to classify required baselines, optional overrides, serial/device-bound generated calibration, runtime evidence, external dependencies, and build artifacts.
- Added `scripts/test_config_baselines.ps1` to local validation. It verifies 28 required clean sources, JSON/template consistency, blank secrets, unique Provider entries, generation contracts, FoundationPose runtime references, Git ignore policy, deterministic initialization, and preservation of existing files.
- Added active `providers.json` to both publishing-script blocklists.

### Validation completed

- The clean-configuration audit passed, including create/re-run preservation tests in an isolated temporary directory.
- All 37 publication JSON files and all 3 YAML files parsed.
- All 77 publication PowerShell files passed parser validation; all 121 Python files compiled.
- 238 Python tests passed: camera 2, Local VIO 30, Test Agent 5, FoundationPose 43, Basic arm 75, Integrated arm 54, and Stationary World-Space Arm Finder 29.
- The isolated FoundationPose default-profile migration/seeding regression passed with the canonical meshes, source, reference atlases, license, and provenance files.
- Manager 3 and Fabric 3 Rust tests passed; Rust formatting passed.
- All 12 repository/component/profile manifests were regenerated; the 467-entry root manifest includes the clean examples and audit, and a second updater run was byte-identical.
- No Provider was started and no robot motion command was submitted during this audit.

### Generated or external state intentionally left without populated templates

- Orbbec accelerometer calibration remains generated only after a valid physical device serial is observed.
- Alignment calibration revisions, captures, masks, screenshots, debug bundles, Basic calibration sessions, logs, and PID files remain runtime evidence/state.
- The FoundationPose upstream checkout/CUDA build, Orbbec SDK/CameraHost binaries, virtual environments, and compiled output remain install/build products.
- API keys remain blank and hosted-model features still require an operator-supplied local key.

## Cross-provider hardening and publication correction — 2026-07-26

### Published implementation corrections

- Commit `171fa1b458835d855c7c6b2d80bfffb08d72cb0b` published immutable alignment-camera reference poses, bounded best-of-two alignment fallback, Integrated controlled-frame and reliable Fabric-command guidance, trajectory-start joint recovery, acknowledged safe-termination launch, and dependency-aware Stop All behavior.
- Commit `12ad6d26d24dbe55093b2733fab357674501f74f` published Basic safe-home lease fencing and late-command rejection plus Manager per-Provider `force_kill_on_stop_timeout` behavior.
- This follow-up publication updates the formal Contracts, root changelog, main-page status, configuration guidance, reusable FoundationPose asset discovery, and this report.

### Validation completed

- 37 existing camera, Local VIO, and Test Agent regression tests passed.
- 75 reBot Arm DM Basic tests passed.
- 54 reBot Arm Integrated tests passed.
- 29 Stationary World-Space Arm Finder tests passed.
- 3 Resource Provider Manager Rust tests and 3 World State Fabric Rust tests passed.
- The latest GitHub source-validation workflow for the implementation correction passed.
- Global Stop All completed after testing, and ports `7001`, `7002`, `7101`, `7102`, `7104`, `7105`, `7106`, `7107`, `8011`, `8012`, `8791`, and `8793` were confirmed closed.
- A clean repository-manifest audit found and repaired 33 stale root entries from the two implementation commits. The updater now hashes canonical LF text on both Windows and Linux.
- All 37 publication JSON files parsed, all 121 publication Python files compiled, and all 856 entries across the root and eleven component/profile manifests verified without a missing file or hash mismatch.
- Two consecutive manifest-updater runs produced identical output. The 32-file publication set passed Git whitespace checks and a targeted secret, credential, device-serial, private-path, and vegetable-cutter-path scan.

These checks were offline software and shutdown validations. No physical arm command was submitted during the publication correction. The earlier supervised vegetable-cutting experiments exposed the transform, staging, and termination problems, but the vegetable-cutting Skill itself remains experimental, incomplete, and outside this public release.

### Contract and asset audit

- Contracts working draft 0.3.9 now requires policy-aware graceful-timeout escalation, explicit-force separation, and operational-lease fencing before Provider-owned safe-home or controlled-stop motion.
- The reusable reBot B601-DM STEP/OBJ sources and prepared meshes in local `config/foundation_pose` were compared with the sanitized canonical profile under `providers/foundation_pose/defaults/rebot_b601_dm`.
- The mesh geometry and retained STEP sources match after text line-ending normalization. The public metadata intentionally replaces absolute workstation paths with portable relative paths and adds publication provenance.
- The Base and Gripper reference atlases were visually inspected and are linked from the main README.
- An identical sanitized copy of the complete 20-file reBot profile is published under `config/foundation_pose`, matching the Provider's configured runtime registry path and providing a directly restorable local baseline.
- The two 20-file profiles are byte-identical (89,211,623 bytes each), and their text assets are pinned to LF so the checked manifests and restore behavior are stable across Windows and Linux Git settings.
- API keys, active `providers.json`, `system.env`, serial-bound calibration, runtime captures, installation caches, model archives, logs, and absolute-path machine metadata remain excluded. A blank `config/api_keys.env.example` is published instead.

### Remaining validation boundary

- Repeat controlled hardware validation before treating the new safe-home and shutdown behavior as physically accepted across power loss, USB loss, Manager loss, and a missed graceful-stop deadline.
- Verify explicit force-stop behavior only in a prepared recovery scenario because it may intentionally remove powered support.
- Validate a future decomposed cutting workflow independently for world-axis registration, RGB-D registration, VLM-guided correction, and bounded cutting execution before publishing it as deployable.

## reBot arm Provider publication supplement — 2026-07-23

- Added Basic 0.1.20 and Integrated 0.7.0 as source-only Providers; `.venv`, caches, runtime logs, and machine-local calibration remain excluded.
- Validated Basic with 73 tests and Integrated with 52 tests using their source-only publication trees.
- Added capability-specific Manager readiness and a provider-local operation catalog.
- Advertised only usable MIT one-shot/continuous and limited unloaded POS_VEL one-shot.
- Kept continuous POS_VEL and arm POS_TOR one-shot available only in the operator GUI as non-discoverable experimental/unstable modes.
- No provider was started and no physical arm command was sent during publication validation.

## FoundationPose v0.3.0 publication supplement — 2026-07-22

- Ran 43 FoundationPose Provider regression tests successfully.
- Verified provider-package publication checks, including secret scanning, required assets, and the complete NVIDIA FoundationPose license.
- Verified live registration with the real Manager and publication of Base and Gripper transform edges into the real Fabric.
- Published the two required FoundationPose checkpoint files through Git LFS; together they are approximately 258 MB.
- Kept the upstream NVLabs source checkout, generated CUDA artifacts, virtual environments, runtime captures, and API keys outside Git.
- The repository-wide `scripts\validate.ps1` workflow still validates the original RGB-D/VIO baseline and does not yet invoke the FoundationPose-specific test suite. Run the Provider validation command documented in `docs\06_VALIDATION.md` in addition to the repository workflow.
- No GitHub Actions result is claimed for this supplement; the recorded checks were run on the Windows development workspace before publication.

## Completed in the cleanup environment

- Compared the documented v0.3.10 source workspace with the supplied working workspace.
- Confirmed all 163 documented source files were present and byte-for-byte unchanged before cleanup.
- Removed generated output, local configuration, device calibration, SDK runtime binaries, runtime state, opaque archives, and unrelated Providers.
- Parsed tracked JSON files.
- Compiled Python source bytecode successfully.
- Ran 37 Python regression tests successfully.
- Built these Python wheels successfully outside the source repository:
  - `orbbec_femto_provider-0.3.1-py3-none-any.whl`
  - `physical_ai_local_vio_provider-0.2.2-py3-none-any.whl`
  - `physical_agent_test_scaffold-0.2.9-py3-none-any.whl`
- Verified repository manifests and Git staged-file whitespace checks.
- Corrected and statically rescanned PowerShell variable interpolation after Windows reported a parser error in the validation script.
- Applied the Manager formatting changes reported by Windows through revision 4. These are formatting-only changes; revision 4 also makes validation run the installed formatter automatically before checking.
- Added a permissive MIT License for original project code and a separate notice that the third-party source/dependency audit remains pending.

## Not compiled in the cleanup environment

- Manager and Fabric Rust binaries: the cleanup container did not include Cargo/rustc and could not install them because external package resolution was unavailable.
- Native CameraHost: requires Windows, Visual Studio MSVC, and the Orbbec SDK 2.8.6 development/runtime files.

Run `scripts\validate.ps1` on the target Windows development machine before upload. By default it requires and compiles the Rust workspace. Add `-BuildNativeCamera` with valid Orbbec SDK paths to compile CameraHost as part of validation.

## Revision 4 validation behavior

The Windows validation script now runs `cargo fmt --all` before `cargo fmt --all -- --check`. This delegates formatting of all Rust source, including `#[cfg(windows)]` branches, to the installed Rust toolchain. After successful validation, `scripts/update_manifests.ps1` regenerates the component and repository SHA-256 manifests so formatting changes are recorded before publication.
