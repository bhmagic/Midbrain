# Changelog

## Unreleased

- Replace the hard-coded link/tool exclusion chain with the active Basic
  assembly-state arm capsules and mounted-effector spheres.
- Publish the exact profile-driven effector spheres with each compiled scene so
  controller diagnostics and the main 3D viewer use the same geometry.
- Apply that same profile-bound self filter to SAM2 semantic cells, preventing
  the mounted tool from returning as a false work-object collision at sample
  zero.

## 0.1.3 - 2026-08-04

- Bind every compiled semantic scene to the exact upstream tracker policy
  revision, Provider instance, boot, and sequence so a prior scene cannot be
  mistaken for a newly requested remap.
- Publish only when the point-cloud or semantic source key changes. A
  semantic-only scene no longer recompiles at the 20 Hz polling rate, removing
  unnecessary Fabric, controller, and 3D-viewer churn.

## 0.1.2 - 2026-08-04

- Accept explicit semantic assertions for their full 10-second upstream
  freshness window so the quarter-rate SAM2 tracker can reduce camera/UI
  contention without creating artificial scene gaps.

## 0.1.1 - 2026-08-04

- Align documentation with the explicit-policy implementation: user/upstream
  `KEEP_OUT` assertions are blocking, while unclaimed depth is ignored
  `PUSHABLE` telemetry unless diagnostic publication is explicitly enabled.

## 0.1.0 - 2026-08-03

- Added the HOT single-owner canonical arm semantic-scene compiler.
- Added simultaneous 0.5 m gripper/20 mm and 1.2 m base/60 mm ROI layers.
- Vectorized voxel aggregation and added producer phase timing so scene
  freshness bottlenecks are visible instead of reported only as stale output.
- Added current arm-link self-filtering, explicit keep-out/workpiece/pushable
  semantics, ignored unclaimed pushable telemetry, short TTLs, and monotonic
  revisions.
- Added native camera BufferRef and Fabric-hosted external point-cloud inputs.
- Added semantic-only degraded fallback for fresh explicit assertions when
  depth geometry is material-limited or empty.
- Added Manager lifecycle, diagnostics, setup, tests, and the finite read-only
  `inspect_arm_semantic_scene` test surface.
