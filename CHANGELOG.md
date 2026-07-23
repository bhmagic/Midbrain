# Changelog

## reBot arm Providers publication (2026-07-23)

- Added reBot Arm DM Basic Controller 0.1.20 and Integrated Controller 0.7.0 as separate Providers with separate virtual environments.
- Added Manager-discoverable capability readiness and a provider-local operation catalog for upstream Skills.
- Marked MIT one-shot and continuous/HOLD_LB usable.
- Marked POS_VEL one-shot limited to paths at or below 20 cm with no payload or high external load.
- Kept POS_VEL continuous and arm POS_TOR one-shot as experimental/unstable local GUI tests and excluded both from Manager capability discovery.
- Documented Fabric Cartesian target/settings staging, operator Engage + Xbox LB authority, latched MIT/POS_TOR gripper behavior, gravity-float, and authoritative safe-home termination.
- Recorded offline validation separately from physical acceptance; no autonomous motion authority is claimed.

## FoundationPose Provider v0.3.0 publication (2026-07-22)

- Added `providers/foundation_pose` as a Manager-discoverable CAD-based 6D object-pose Provider.
- Added independent Base and Gripper targets for the reBot B601/ER1.6 arm, with camera-relative transforms published into the Fabric.
- Added a GUI-assisted initialization workflow using OpenAI visual localization, two positive object points, cropped SAM2 segmentation, and operator review before tracking.
- Added tested mask refinement defaults: median Lab distance 30 with radius-2 dilation for the Base, and median RGB 10% drift with radius-2 dilation for the neon-green Gripper root.
- Added mesh preparation and renderer tooling, reusable reference renders, prepared-asset caching keyed by source content and preparation settings, and selectable tracking rates.
- Validated 43 Provider tests plus live Manager/Fabric registration and transform publication checks.
- Published the two required FoundationPose checkpoint files through Git LFS with the complete NVIDIA FoundationPose license. These checkpoints are restricted to non-commercial research and evaluation use.
- Kept camera-to-world alignment outside the Provider: the Provider publishes measurements, while a future bounded Skill must aggregate stationary observations, resolve symmetry, and publish an independently authoritative alignment transform.

### Revision 4 packaging corrections

- Applied the remaining Windows-gated `rustfmt` change in the Manager process launcher.
- Changed `scripts/validate.ps1` to run `cargo fmt --all` before the strict formatting check, preventing future platform-gated formatting drift.
- Added `scripts/update_manifests.ps1`; successful validation now refreshes component and repository SHA-256 manifests after formatting and builds.

## 0.3.10 — GitHub source cleanup (2026-07-20)

- Consolidated the Manager, Fabric, contracts, Orbbec Femto Bolt Provider, Local VIO Provider, and Test Agent into one source-only repository.
- Added a canonical documentation sequence and two functional tutorials.
- Added source validation, Python wheel build, GitHub publishing, and CI workflows.
- Corrected Windows PowerShell parsing in `scripts/validate.ps1` by delimiting `${LASTEXITCODE}` before a colon.
- Applied the complete Manager `rustfmt` layout reported by `cargo fmt --all -- --check`, including the process-ID expression missed in revision 2.
- Added the MIT License for original project code and a pending third-party audit notice.
- Declared MIT package metadata for the Rust crates and Python distributions, with the license text included in each Python wheel.
- Retained `Cargo.lock` for reproducible Rust application builds.
- Corrected stale component-version wording in package documentation.
- Excluded machine-local configuration, serial-bound calibration, API keys, proprietary SDK binaries, generated output, runtime state, and unrelated robot-arm Providers.
- Verified 37 Python regression tests and built all three Python wheels.

## Integrated component baseline

- Manager/Fabric 0.3.0
- Orbbec Femto Bolt Provider 0.3.1
- Local VIO Provider 0.2.2
- Test Agent 0.2.9
- Contracts working draft 0.3.8
