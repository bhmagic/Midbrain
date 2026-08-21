# Changelog

## Unreleased

- Remove the post-SAM2 VLM mask-quality call and its three-attempt rejection
  loop. Required SAM2 masks still fail when missing or empty.
- Accept explicit policies with zero `KEEP_OUT` objects so mapping/location
  semantics do not invent collision geometry.

## 0.3.2 - 2026-08-19

- Generalize the one-shot prompt contract from exactly two positive points to
  one through four, allowing each independent Skill-level VLM judgment to
  seed its own SAM2 mask without duplicating a point.
- Keep mask review, voting, dilation, and robot semantics outside the Provider.

## 0.3.1 - 2026-08-19

- Accept optional negative point prompts in the generic single-image SAM2
  capability, pass them to the native predictor with background labels, and
  prefer multi-mask candidates that exclude those points.
- Preserve negative-point prompt provenance without moving semantic target
  selection out of the calling Skill.

## Earlier unreleased work

- Add the single-image `perception.image.sam2.segment` Provider capability.
- Keep VLM seed-box and point selection outside the Provider so Skills own
  semantic prompt intent and this Provider remains a prompted segmenter.
- Pin clean setup to the audited upstream SAM2 revision and preserve RGB/mask
  artifact digests in the one-shot result contract.

- Replace the two-tier semantic collision output with a bounded hand-centric
  4 pi projection using 4,096 near-uniform spherical Fibonacci directions by
  default. Retain one nearest visible hit per occupied direction and scale its
  sphere radius with range while preserving a 5 mm close-range floor.
- Publish shared angular profile and timestamped hand-origin metadata once per
  observation, and round sphere geometry to micrometre precision, instead of
  repeating projection diagnostics on every sphere.
- Publish five-second `WORK_OBJECT` visible-surface AABBs aligned to the
  canonical arm-base axes so agents can refer to deterministic corners without
  treating the extent as a tracked solid model. Obstacles remain sphere-only.
- Make the SAM2 tracking rate a fixed 1-4 Hz setting with a 1 Hz default. VLM
  annotation keeps its independent motion/confidence refresh policy.
- Erode reviewed geometry masks by a registered-depth metric margin before
  point-cloud projection: 10 mm for work objects and 20 mm for obstacles by
  default. Semantic fusion, collision spheres, and work-object AABBs now share
  this eroded source mask. Use one constant pixel radius per object, calculated
  from that mask's mean registered depth.
- Publish a current-policy invalid mapping result with a structured external
  prerequisite when the camera-to-arm-base transform is unavailable, while
  keeping reviewed 2D mask tracking active.

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
