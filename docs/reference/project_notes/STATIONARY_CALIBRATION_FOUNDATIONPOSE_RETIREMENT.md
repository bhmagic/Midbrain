# Stationary Calibration and FoundationPose Retirement

Status: Phase 2 dual-route evaluation; not enforced

## Ownership decision

`calibrate-stationary-workcell` owns the finite job of locating the stationary
robot base, using the gripper or end effector to resolve/correct orientation,
and publishing a versioned world-to-arm registration. FoundationPose is an
estimator implementation used by that job, not a useful continuously resident
real-time Provider.

Local VIO remains a separate localization Provider. VIO means
visual-inertial odometry: it combines camera observations and IMU propagation
to estimate the camera/body pose in a local world frame. During stationary
calibration it establishes the camera/world relationship and epoch. The
calibration Skill rejects an epoch change while estimating the base. A later
phase may freeze the validated stationary transform and stop VIO for a finite
downstream session, as the legacy cutting prototype already demonstrates.

## Phase 2 routes

- `PROVIDER_COMPATIBILITY` is the unchanged default. The Skill starts bounded
  FoundationPose sessions through Manager, consumes Fabric pose observations,
  and stops the Provider when no foreign sessions remain.
- `SKILL_LOCAL` owns the estimator lifetime inside the finite Skill. It consumes
  fresh RGB-D frames, queries the timestamped VIO transform for every sample,
  rejects a world/epoch change, resets attempt state, and returns the same
  camera/world sample structure used by the existing fusion and validation.
- Automatic route fallback is disabled. The compatibility route remains
  explicitly selectable.

The initial local route temporarily imports the tested backend and model
registry library from the existing FoundationPose package. It does not import
or run the Provider process, Manager registration, Fabric pose publishing,
session scheduler, or native desktop GUI. After guarded comparison, the backend
library can move under the Skill or a neutral estimator library and the
Provider wrapper can be retired without changing the calibration algorithm.

## Required comparison before default switch

Compare both routes on identical stationary scenes for:

- first-result and total latency;
- base translation/orientation agreement and projected-box validation;
- success after a rejected first attempt;
- RGB-D stall, VIO epoch change, cancellation, and motion-inhibit renewal loss;
- GPU memory release/reuse across repeated GUI runs;
- exact calibration evidence and provenance.

The default may switch only after these comparisons pass. Provider discovery
and configuration should be removed in a later policy-enforcement phase, not
as part of this shadow interconnection.
