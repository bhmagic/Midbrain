# Version History and Engineering Decisions

## v0.3.1 — First Space Cognition milestone

Components included Local VIO v0.1.0 and Test Agent GUI v0.2.0.

- Added transform graph, Local VIO Provider, Initialize Space Cognition Skill, motion inhibit, body pose, and world-frame point-cloud GUI.
- Visual pose authority used raw ORB, RGB-D geometry, and PnP.
- IMU supplied startup gravity and limited rotation consistency checks.
- Hardware showed useful room scanning and consistent point accumulation during moderate pivots.
- This implementation deviated from the original inertial-first requirement; it was RGB-D visual odometry with IMU assistance.

## v0.3.2

- Added continuous gravity leveling, world-down arrow, clearer initialization state, and reset/map resume sequencing.
- The gravity correction was too willing to alter a valid visual pose and damaged panning.

## v0.3.3

- Fixed post-reset observation rejection by preserving monotonic Provider sequence across new session epochs.
- Added stronger stationary gravity recovery and orthographic isometric visualization.
- The map reset path improved, but pose-driving gravity still disturbed visual tracking.

## v0.3.4

- Restricted gravity during motion, added separate tracking/gravity/map/reset diagnostics, and rendered a camera frustum.
- Panning remained worse than the v0.3.1 baseline because gravity still modified valid tracking under some quiet classifications.

## v0.3.5

- Structurally restored the original raw ORB/PnP path during valid visual tracking.
- Added adaptive circular local-contrast normalization as a secondary low-light candidate.
- Added an explicit gravity adjustment light.
- Set a strict 0.005 rad/s gravity gate, which was too low for practical sensor noise and often prevented correction.

## v0.3.6

- Allowed bounded gravity leveling during valid tracking when the IMU was genuinely quiet.
- Added startup gyro bias estimation and adaptive residual-noise thresholding.
- Chose nominal 0.012 rad/s with an adaptive effective threshold.
- Kept raw visual baseline and low-light candidate behavior unchanged.

## v0.3.7

- Made reset acceptance independent of a best-effort immediate Fabric status publication.
- Waited for motion-inhibit acknowledgement before initialization.
- Replaced scalar visual/gyro angle comparison with full 3D rotation disagreement.
- Added gyro-seeded PnP and short gyro-only rotation propagation with translation hold when visual rotation was untrustworthy.
- Exposed a separate rotation-estimator indicator.

## v0.3.8 — Architectural correction

Local VIO advanced to v0.2.0.

- Replaced visual-first pose authority with a 15-state inertial error-state filter.
- Every ordered IMU sample propagated orientation, position, velocity, gyro bias, accelerometer bias, and covariance.
- RGB-D became a gated metric correction measurement.
- Added synchronized IR/native-depth fallback.
- Added high-rate non-committing pose prediction.
- Preserved gravity tuning, reset/epoch fixes, and low-light visual frontend.

## v0.3.9

Local VIO v0.2.1.

- Fixed startup selection of gyro samples around an unrelated RGB timestamp.
- Preferred SDK system timestamps consistently.
- Selected initialization data in the common IMU time domain.
- Added accel/gyro history counts, timestamp skew, and explicit initialization blockers.

## v0.3.10 — Current baseline

Local VIO v0.2.2 and Test Agent v0.2.9.

- Removed the impossible assumption that 80 samples fit inside a fixed 1.5-second window.
- Selected newest initialization samples by count with a five-second stale-history ceiling.
- Added selected window counts and inferred sample rates to the GUI.
- Regression reproduced the observed 1,198/1,198 histories at 50 Hz.
- Operator subsequently reported the system as performing well.

## FoundationPose Provider v0.3.0 — 2026-07-22

- Added a Manager-discoverable CAD-based object-pose Provider for independent reBot Base and Gripper targets.
- Added GUI-assisted OpenAI box/point localization, cropped SAM2 segmentation, operator review, and target-specific mask refinement.
- Replaced defective face-sampled CAD references with full-geometry renders using correct triangle topology and consistent multi-angle scale.
- Added prepared-asset caching keyed for future CAD models and selectable tracking-rate controls.
- Published camera-relative object observations and transform edges into the Fabric, validated by 43 tests and a live Manager/Fabric integration check.
- Published the required NVIDIA checkpoints with Git LFS and retained the complete non-commercial research/evaluation license.
- Preserved the authority boundary: FoundationPose measures camera-relative object poses; a bounded alignment Skill must establish any camera-to-world transform.

## Decisions to preserve

- Keep Manager, Fabric, Resource Provider, Skill, Observation, Capability, and BufferRef terminology.
- Keep large payloads outside Fabric.
- Preserve serial-bound calibration and do not overwrite `config` during overlays.
- Preserve orthographic/isometric viewer and explicit estimator-stage indicators.
- Preserve adaptive gravity gating and rotation-only leveling.
- Preserve raw visual baseline; preprocessing and IR are optional correction candidates.
- Preserve inertial-first state propagation. Do not return to camera-as-primary pose authority.
