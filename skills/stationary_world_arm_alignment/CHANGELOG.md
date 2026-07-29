# Changelog

## 0.7.1 - 2026-07-29

- Added local reviewed-candidate activation support with stable decision
  identity, signed reviewer evidence, bounded request timeouts, and the
  operator-facing `scripts/review_and_activate.py` helper.
- Ordered calibration startup so the camera and Local VIO can become current
  before Integrated or Basic arm authority is requested. Calibration remains
  stateful but nonphysical and does not command the arm.
- Preserved FoundationPose as a compatibility route owned by this finite
  Skill. It is not advertised as a real-time tracking Provider or a continuous
  source of arm-base truth.
- Added discoverable finite-Skill metadata for stationary workcell calibration
  while keeping activation, provenance, freshness, quality, and motion-inhibit
  gates outside the model prompt.
- Completed and activated calibration
  `20260729T080635Z-7286b758` as
  `world/stationary_camera/20260729T080635Z-7286b758`, bound to VIO epoch
  `aed3c599-6934-4147-86c5-1e98a0cd99f0`. Its reported error bounds are
  0.002728 m and 0.028931 rad.
- Recorded the symmetric-yaw ambiguity from the base estimator. The Skill
  selected the 180-degree alternative using current kinematic gripper
  evidence.
- Recorded an underexposed calibration image limitation. A deterministic gamma
  copy was used only for review; the original RGB-D observation and hashes
  remain authoritative.
- Unified the browser development GUI around the shared dark
  white/gray/black palette while retaining source-image and semantic
  status/warning colors.
- Documented the remaining axis-alignment limitation: the active workcell
  transform relates camera-world and arm-base axes, but natural-language
  directions still require explicit gravity, frame, transform-revision,
  timestamp, and uncertainty semantics.

## 0.7.0

- Added reviewed calibration candidates and Manager-enforced activation.
- Added finite Agent discovery metadata and explicit stationary-scene scope.
- Added current camera, calibration, VIO epoch, transform, motion-inhibit, and
  quality provenance to the motion-usable activation boundary.

Earlier history is described in `README.md` and
`docs/EVALUATION_AND_DESIGN.md`.
