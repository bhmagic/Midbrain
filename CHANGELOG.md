# Changelog

## Cross-provider alignment and shutdown hardening (2026-07-26)

- Updated the Stationary World-Space Arm Finder to publish the immutable full `vio_from_camera_reference` pose captured with alignment RGB-D evidence. Downstream fixed-camera Skills no longer need to combine a saved alignment with a later, drifted VIO pose.
- Changed the two-attempt FoundationPose fallback to retain the geometrically better bounded observation when neither attempt reaches the strict VLM threshold, while preserving non-BAD geometry, projection-coverage, orientation, and confidence gates.
- Documented Integrated controlled-frame semantics: `ik_location` names the desired acting-point pose, and Integrated applies `ik_offset` internally. Upstream Skills must not apply the tool offset a second time.
- Added reliable Fabric command staging guidance using producer/boot/sequence identity, `accepted_count`, bounded republishing with fresh sequence and timestamps, and terminal rejection handling.
- Added Integrated workspace-envelope publication, first-command recovery for a measured joint slightly outside its operational range, and acknowledged safe-termination launch behavior.
- Made global Stop All dependency-aware: Integrated is stopped before Basic, safety-critical stop confirmation is required, and powered arm support is preserved when Manager-based safe shutdown cannot be confirmed.
- Added Manager `force_kill_on_stop_timeout` as a per-Provider policy. It remains enabled by default, while the supplied Basic arm registration disables automatic timeout escalation so a missed graceful stop does not silently remove powered support.
- Updated Basic safe-home to revoke and fence the operational lease, clear pending work, and reject late operational commands before the first protective MIT frame.
- Updated formal Contracts to distinguish automatic graceful-timeout escalation from authorized explicit force-stop and to require fencing before Provider-owned protective motion.
- Made repository manifest generation deterministic across Windows and Linux by canonicalizing text to LF before hashing; repaired 33 stale root-manifest entries left by the two implementation commits.
- Exposed the already-published sanitized FoundationPose reBot CAD profile and Base/Gripper reference atlases from the main README. Added a root API-key template and documented why serial-bound calibration, absolute local paths, and camera captures remain excluded.
- Added an identical sanitized FoundationPose runtime/restore profile under `config/foundation_pose`, matching the path used by the supplied Manager configuration so fresh checkouts do not depend on a seeding prompt.
- Added root recovery examples for `system.env` and `providers.json`, made the core initializer create a blank `api_keys.env`, synchronized package fallback templates, and made canonical Provider entries inherit Manager/Fabric endpoints plus the camera mapping name from `system.env`.
- Added a configuration-baseline inventory and automated clean-checkout audit covering generation, preservation, blank secrets, ignore rules, Provider-entry consistency, arm/Skill templates, and FoundationPose runtime references.
- Added active `providers.json` to the publication blocklist so a force-added machine-local Provider registry cannot be uploaded accidentally.
- Recorded the corrective validation and remaining hardware boundary in `BUILD_REPORT.md`. The local vegetable-cutting experiment remains non-deployable and is intentionally not published as a production Skill.

## Stationary World-Space Arm Finder v0.4.0 (2026-07-24)

- Added `skills/stationary_world_arm_alignment` as a finite Skill with an isolated virtual environment, CLI, monitoring GUI, schemas, and regression tests.
- Added the three concrete upstream modes `foundation_base_gripper`, `foundation_base_vlm_gripper`, and `vlm_gripper_only`, plus automatic selection.
- Added upright base correction, projected 3D-box/axis VLM validation, one fresh FoundationPose retry, and three-inference closest-pair voting for large VLM adjustments.
- Added immediate RGB-D shared-memory copying with fresh-bundle retries when a camera `BufferRef` slot is recycled.
- Added result schema version 2 with explicit base/gripper source contracts and separate semantic labels for the FoundationPose gripper model origin and VLM RGB-D foremost-beak point.
- Added on-demand Provider requests and bounded FoundationPose shutdown while retaining camera, VIO, and arm-pose inputs for other consumers.

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
- Contracts working draft 0.3.9
