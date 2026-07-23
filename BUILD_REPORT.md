# Build and Validation Report

Date: 2026-07-20

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
