# Changelog

## 0.2.1 - 2026-08-04

- Represent reviewed visible KEEP_OUT depth as boundary-tangent collision
  spheres. Dominant planar surfaces place sphere volume behind the plane;
  nonplanar surfaces use a camera-ray tangent fallback. This preserves the
  configured 20/60 mm sphere radii without spilling the full radius into free
  space above a table.
- Publish the measured surface center and selected boundary mode with each
  tracked assertion for diagnostics and viewer evaluation.
- Align the provider runtime, package, manifest, and VERSION metadata.

## 0.2.0 - 2026-08-04

- Require a VLM quality decision on the original RGB, reviewed SAM2 mask, and
  registered depth before a new policy revision can fuse collision spheres.
- Retry VLM annotation, SAM2 segmentation, and VLM mask review up to three
  complete attempts; publish an explicit invalid zero-sphere result after the
  third rejection.
- Clip declared masks to tight prompted regions and positive-point
  depth-connected components to prevent support/floor and nearby-object spill.
- Invalidate persistent voxels for every explicit remap epoch, including a
  repeated user request with identical wording.
- Expose exact RGB, depth, and reviewed-mask visualization channels for the
  Agent mapping result.
- Declare camera and Basic arm state as Manager-owned HOT dependencies and use
  a 15-second heartbeat allowance for synchronous VLM mask review.

## 0.1.0 - 2026-08-04

- Added the HOT explicit-policy SAM2 semantic scene tracker with persistent
  voxel fusion, arm-mask subtraction, adaptive rates, and Fabric assertions.
