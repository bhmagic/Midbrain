# Changelog

This file records release-level outcomes. Current Provider-versus-Skill and
frame terminology is defined in
[Midbrain integration](docs/MIDBRAIN_INTEGRATION.md); Git history retains the
development-level detail.

## Unreleased

- Allowed a three-minute Manager heartbeat window for the measured slow GPU
  cold path without weakening pose quality, session, motion-inhibit, or
  resource checks.
- Required explicit native optical convention metadata and published
  `camera_system_x/y/z` axis names on camera-relative measurements.
- Made default-profile repair reproduce the canonical persistent layout while
  preserving existing registries and custom files.
- Made the documented post-setup publication validator self-contained by
  installing its test extra, and aligned default-profile integrity checks with
  the repository's cross-platform canonical text hashing policy.

## 0.3.0

- Added validated pixel and normalized bounding-box initialization for
  estimate, track, and relocalize requests.
- Added the model-generic Provider development UI and retained the older
  robot-specific VLM + SAM2 GUI as a compatibility diagnostic.
- Added reviewable SAM2 initialization masks, manual correction, prepared CAD
  reference atlases, independently selectable tracking rates, and a bounded
  content-fingerprinted prepared-estimator cache.
- Added Provider-local pinned SAM2 setup and Windows subprocess environment
  normalization without changing the FoundationPose inference algorithm.

## 0.2.4

- Bundled checksum-verified official refiner and scorer checkpoints for
  offline release installation and documented their provenance and Git LFS
  constraint.

## 0.2.3

- Added resumable selective checkpoint recovery and a persistent install cache
  that survives clean Provider reconstruction.

## 0.2.2

- Added validation for agreement among `VERSION`, manifest, Python metadata,
  and runtime version surfaces.

## 0.2.1

- Fixed PowerShell 5.1 migration of an existing default Base + Gripper model
  registry.

## 0.2.0

- Added the complete default reBot B601-DM Base and Gripper reporter profile,
  stable observed frames, semantic roles, camera-relative transform
  provenance, retained CAD sources, and license/modification records.
- Added non-destructive persistent profile seeding, custom rigid-CAD
  preparation tools, package hygiene, and Manager/Fabric documentation.

## 0.1.3

- Added the guarded native-Windows temporary-mesh compatibility patch and
  both fast-update and clean-reinstall paths.

## 0.1.2

- Accepted BOM and non-BOM registries, kept persistent model configuration
  outside the Provider, and preserved full-entry Manager registration.

## 0.1.1

- Added Manager-envelope normalization, Provider-local dependencies,
  persistent model registry loading, and replace-by-ID registration.
