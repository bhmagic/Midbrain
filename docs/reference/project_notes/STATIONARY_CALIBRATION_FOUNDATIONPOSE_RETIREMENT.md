# Stationary Calibration and FoundationPose Retention

Status: Superseded design note retained under its historical filename

## Ownership decision

FoundationPose will not be retired. It remains a finite, slow initializer and
diagnostic estimator, with its compatibility Provider retained for guarded
comparison and downstream compatibility. It is not continuously resident and
is not the generic/default alignment route.

The regular Agent may start it only when the operator supplies the exact
request `Use FoundationPose to establish the stationary world-to-arm-base
transform.` Generic requests such as “establish both axes” must not invoke it,
and movement-alignment failures must not fall back to it automatically.

The ordinary future path is the gripper-motion workflow in
[`../../13_GRIPPER_MOTION_ARM_ROOT_ALIGNMENT.md`](../../13_GRIPPER_MOTION_ARM_ROOT_ALIGNMENT.md).
That finite workflow will collect controller/FK and RGB-D correspondences,
produce a versioned candidate, and leave activation and rollback to Manager.

Local VIO remains a separate localization Provider. VIO means
visual-inertial odometry: it combines camera observations and IMU propagation
to estimate the camera/body pose in a local world frame. During stationary
calibration it establishes the camera/world relationship and epoch. The
calibration Skill rejects an epoch change while estimating the base. A later
phase may freeze the validated stationary transform and stop VIO for a finite
downstream session, as the legacy cutting prototype already demonstrates.

## Retained explicit routes

- `FOUNDATIONPOSE_SKILL` owns the estimator lifetime inside the finite Skill. It consumes
  fresh RGB-D frames, queries the timestamped VIO transform for every sample,
  rejects a world/epoch change, resets attempt state, and returns the same
  camera/world sample structure used by the existing fusion and validation.
- `PROVIDER_COMPATIBILITY` starts bounded FoundationPose sessions through
  Manager, consumes Fabric pose observations, and stops the Provider when no
  foreign sessions remain.
- `SKILL_LOCAL` remains a compatibility spelling for `FOUNDATIONPOSE_SKILL`.
- No route is an automatic Agent-facing default. Automatic fallback is
  disabled, and both finite and Provider compatibility execution are explicit.

The initial local route temporarily imports the tested backend and model
registry library from the existing FoundationPose package. It does not import
or run the Provider process, Manager registration, Fabric pose publishing,
session scheduler, or native desktop GUI. After guarded comparison, the backend
library can move under the Skill or a neutral estimator library without
removing the retained compatibility Provider contract.

## Required comparison with movement alignment

Compare both routes on identical stationary scenes for:

- first-result and total latency;
- base translation/orientation agreement and projected-box validation;
- success after a rejected first attempt;
- RGB-D stall, VIO epoch change, cancellation, and motion-inhibit renewal loss;
- GPU memory release/reuse across repeated GUI runs;
- exact calibration evidence and provenance.

The movement-based workflow may become the normal generic alignment path only
after these comparisons and its own held-out correspondence tests pass.
FoundationPose remains available under the exact invocation regardless of that
switch. Provider discovery and configuration should not be removed merely
because the movement path becomes faster.
