# Changelog

## 0.2.0 - 2026-07-30

- Replaced the ambiguous `camera_point_m` result with named
  `camera_system_x`, `camera_system_y`, and `camera_system_z` components.
- Require explicit native optical and convention-V2 world metadata before
  deprojection and transform use.

## 0.1.0 - 2026-07-29

- Extracted RGB/depth-to-3D registration from the local cutting prototype into
  a finite read-only Skill.
- Added independent RGB, IR, native-depth, and registered-depth grid support,
  including differing resolution, aspect ratio, valid boundary, alignment
  metadata, and frame timestamps.
- Kept large frames in shared memory. Fabric carries references, timestamps,
  calibration/alignment metadata, provenance, and other small values.
- Added generic shared-memory route selection with an explicitly declared
  camera-provider-specific direct fallback.
- Added robust median, closest-to-camera, and nearest-valid-pixel depth
  policies plus exact-timestamp world-transform lookup.
- Made freshness a consumer-Skill policy. Fabric supplies observation time and
  provenance but does not decide whether the result is fresh enough after
  VLM/DLNN processing.
- The Skill does not publish transforms, command hardware, or authorize
  physical motion.
