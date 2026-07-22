# Changelog

## 0.3.0

- Added validated bounding-box initialization for `estimate`, `track`, and `relocalize` requests. Gemini-compatible `box_2d` coordinates use `[ymin, xmin, ymax, xmax]` normalized to 0–1000 and are rasterized as a binary mask at the live RGB resolution.
- Added the discoverable `perception.object_pose.bounding_box_init` capability and expanded the request schema with pixel and normalized coordinate spaces plus optional box padding.
- Added a provider-local native GUI that starts Midbrain, the RGB-D camera, and FoundationPose through existing lifecycle interfaces; shows RGB and pose-axis overlays; asks OpenAI GPT-5.6 Luna for Base and Gripper boxes; allows drag correction; and starts long-lived TRACK sessions through Manager.
- Added fixed-scale multi-view CAD reference rendering for Luna using the bundled reBot Base and Gripper meshes.
- Added unit coverage for bounding-box validation/rasterization and Luna structured-response parsing.
- Replaced the GUI's rectangular initialization with reviewable SAM2.1 Base+
  masks prompted by each VLM box and two definitely-inside foreground points.
- Added OpenAI GPT-5.6 Luna proposals, manual point correction, a frozen review
  frame, and full-resolution mask PNG handoff through the existing `mask_path`
  request field.
- Added median-Lab Base mask growth with distance threshold 30 and median-RGB
  Gripper mask growth with 10% channel tolerance. Both refinements retain only
  seed-connected regions and apply a two-pixel-radius dilation inside the
  padded Luna crop.
- Added independent rate selectors: Base up to 10 Hz and an experimental
  Gripper request limit up to 60 Hz.
- Documented Skill/Agent discovery, the two camera-relative transform edges,
  and the downstream camera-alignment boundary for world-space composition.
- Added a bounded, content-fingerprinted prepared-estimator cache that supports
  arbitrary future CAD registry entries and invalidates on geometry or metadata
  changes.
- Added pinned, checksum-verified provider-local SAM2 setup while keeping SAM2
  source/checkpoints out of the publication archive.
- Added Windows subprocess environment normalization so the provider launcher
  tolerates hosts containing both `Path` and `PATH` without changing Midbrain.
- No Midbrain core, camera Provider, persistent CAD/config, or FoundationPose inference-algorithm changes.

## 0.2.4

- Bundled the exact official FoundationPose refiner and scorer checkpoint files supplied from the upstream Google Drive release, so release installation no longer depends on Google Drive availability.
- Added SHA-256 and exact-size validation for all four bundled checkpoint files.
- Fast update repairs a missing runtime from the bundled payload and populates the persistent install cache.
- Complete clean reinstall reconstructs the Provider and installs checkpoints from the bundled payload in offline mode.
- Added checkpoint provenance documentation under `third_party/nvlabs_foundationpose_weights`.
- Added publication guidance for the 190 MB scorer file: GitHub publication requires Git LFS or a release-asset strategy.
- No Midbrain core changes, CAD changes, semantic-transform changes, or FoundationPose inference-algorithm changes.


## 0.2.3

- Fixed clean-install recovery when the official NVLabs Google Drive folder download fails part-way or is throttled.
- Added selective checkpoint discovery using `gdown --folder --json` and per-file resumable retries instead of downloading the entire Drive folder in one operation.
- Added persistent third-party weight cache under `config/foundation_pose/install_cache/nvlabs/FoundationPose/weights`.
- Fast update now verifies/restores/downloads missing official weights, so it can repair an interrupted clean install without rebuilding CUDA/native dependencies.
- Clean reinstall salvages valid existing weights into the persistent cache before deleting the disposable Provider directory.
- Added offline unit tests for checkpoint listing/validation.
- No Midbrain core changes, no CAD changes, and no FoundationPose inference algorithm changes.


## 0.2.2

- Fixed publication release-version drift: `VERSION`, `manifest.json`, `provider.py`, and `python/pyproject.toml` now all report 0.2.2.
- Extended static publication validation to verify all release-version surfaces agree.
- Added a pytest regression for release-version consistency.
- Retains the v0.2.1 Windows PowerShell Base+Gripper persistent-registry migration fix.
- No Midbrain core changes and no FoundationPose inference/runtime algorithm changes.


## 0.2.1

- Fixed Windows PowerShell 5.1 default-profile migration when merging an existing Base + Gripper registry.
- `seed_default_models.ps1` now explicitly converts the generic merged list with `ToArray()` before assigning `models`.
- Added an isolated PowerShell regression test that reproduces the exact existing two-model reBot migration path.
- No Midbrain core changes and no FoundationPose inference/runtime algorithm changes.


## 0.2.0

Publication-ready Provider package.

- Add a complete default Seeed reBot B601-DM geometry profile.
- Make Base and Gripper reporter roles explicit in `models.json`.
- Add stable default observed child frames while preserving `session_epoch`.
- Publish `object_role` and configured `semantic_frame` metadata with object
  pose measurements and transform provenance.
- Keep the Provider a raw visual measurement authority; symmetry/task
  disambiguation remains external.
- Add selected upstream reBot STEP source, CERN-OHL-W-2.0 license text,
  provenance, and modification notices for distributed CAD derivatives.
- Add offline CAD preparation helpers for adapting another rigid robot target.
- Seed persistent `config/foundation_pose` from Provider-owned defaults without
  overwriting existing geometry; migrate only Base/Gripper role and stable-child
  metadata when an existing registry is recognized as the default reBot profile.
- Rewrite documentation around Manager lifecycle, Fabric timestamp semantics,
  default reBot reporters, licensing, and custom-CAD preparation.
- Exclude recorded captures, backups, virtual environments, NVLabs checkout,
  weights, caches, logs, and debug output from the Git-ready Provider tree.
- Retain the v0.1.3 native-Windows NVLabs temporary-mesh compatibility fix.

## 0.1.3

- Fix native-Windows ESTIMATE initialization against the pinned NVLabs FoundationPose checkout.
- Patch upstream `estimater.py` so its temporary centered OBJ uses `FOUNDATIONPOSE_TEMP_DIR` or Python's per-user temporary directory instead of the Linux-only `/tmp/<uuid>.obj` path.
- Verify the compatibility patch before loading NVLabs FoundationPose on Windows.
- Add regression tests for patch application, idempotency, and refusal to patch an unexpected upstream source revision.
- Provide both a fast code-overwrite updater and the complete clean-reinstall path.

## 0.1.2

- Accept UTF-8 JSON model registries both with and without a BOM using `utf-8-sig`.
- Keep the persistent registry under `config/foundation_pose/models.json`.
- Preserve the full-entry Provider registration pattern used by Midbrain Providers.
- Keep Midbrain and Provider processes stopped at installer exit.

## 0.1.1

- Normalize Midbrain Manager request envelopes by merging request `payload` into the effective Provider request.
- Keep compatibility with direct top-level Provider requests.
- Use a Provider-local `.venv`.
- Use a Provider-local NVLabs FoundationPose checkout.
- Read the object model registry from persistent `config/foundation_pose/models.json`.
- Register by replacing the full Provider entry by `id`, following Midbrain's existing Provider registration pattern.
