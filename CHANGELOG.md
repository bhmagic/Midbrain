# Changelog

## 0.3.16 — Phase 5 guarded-agent checkpoint (2026-07-29)

- Added formal Agent Skill discovery and large-data route-advertisement contracts. Agent selection follows descriptive Skill metadata, with explicit provider IDs retained as a fallback.
- Kept RGB-D payloads in shared memory while publishing timestamps, buffer references, channel geometry, alignment metadata, and direct Orbbec fallback routes through Fabric.
- Added Manager authorization decisions, lease lineage, denial/invalidation handling, and global safe-termination ownership.
- Moved reusable Cartesian planning, velocity limiting, singularity checks, workspace checks, collision preview, endpoint-jump checks, and command audit into the Integrated Controller.
- Added a durable audit copy of submitted control intent and the controller/provider acknowledgements without routing the latency-sensitive control loop through Fabric.
- Added the Stationary World-Space Arm Alignment, RGB-D Registration, Tool-to-Control-Frame Registration, and General VLM Observation Skills, with browser-based development interfaces following the neutral dark theme.
- Narrowed the OpenAI Agents SDK execution surface to a reviewed decision ID. The model selected the Skill; policy and motion authority remained deterministic and server-side.
- Completed an operator-observed no-contact toilet-paper standoff trial, a measured 12.67 cm vertical lift from the tested pose, safe-home, gravity-float verification, and full service shutdown.
- Preserved error behavior in which loss/fault paths prefer gravity float, while non-error control retains the current mode until a later command.
- Documented remaining Agent SDK roadblocks, component changes, authority lineage, and the unresolved Cartesian-axis/alignment problem under `docs/reference/project_notes`.
- Kept machine-local `config/api_keys.env`, `config/system.env`, active `config/providers.json`, calibration, captures, logs, and runtime state outside the publication.
- Synchronized public configuration templates with blank secret fields, fixed
  an ambiguous Orbbec test import, and added monorepo test source setup.
- Preserved the protected legacy GitHub workflow because the publishing token
  lacked the separate `workflow` OAuth scope. On its Linux host, its exact
  Python command passes 101 tests and skips thirteen: three Agents-SDK-only
  modules plus ten Windows named-shared-memory replay tests. The full Windows
  publication matrix was validated independently.
- Passed the exact stopped `469/469` publication matrix and the wider
  `578/578` local matrix, plus source parsers, Rust formatting, and Rust
  release build.

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
