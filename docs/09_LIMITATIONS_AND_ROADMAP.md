# Limitations and Roadmap

## Spatial convention is implemented; physical qualification remains

The software contract now defines ordinary 3D language in a canonical world
frame: +X front, +Y left, and +Z up opposite measured gravity. Motion Skills
resolve that vector through a current timestamped transform before arm-base IK.
Raw arm axes require explicit `ARM_BASE_*` names. Raw camera optical components
retain X-right, Y-down, Z-forward geometry but use explicit
`camera_system_x/y/z` names. Two-dimensional image directions are a separate,
explicit vocabulary.

Old Y-up VIO epochs, maps, points, calibration candidates, and previews are
rejected rather than reinterpreted. A missing world-to-arm transform fails
closed; a bounded development identity assumption requires an explicit
installation attestation. Camera-relative 3D language requires explicit
gravity-leveled semantics and ignores camera pitch and roll.

This implementation still requires physical cross-axis qualification on each
installation, including side-mounted arms, camera-heading degeneracy, transform
age/revision faults, and post-restart epoch changes. Natural-language Cartesian
motion therefore remains previewed and operator-authorized. See
[Spatial Frame Convention](../contracts/14_spatial_frame_convention_v2.md).

## Current estimator limits

- The Local VIO backend is a Python reference 15-state ESKF with pose-level RGB-D/IR corrections.
- It is not a feature-level MSCKF or nonlinear fixed-lag optimizer.
- Camera/IMU time offset is not estimated online.
- Long visual outages can accumulate position and velocity drift.
- Noise, covariance, and gating parameters have not been optimized against recorded ground-truth trajectories.
- IR fallback geometry and accuracy need physical measurement in the target environment.

## Infrastructure limits

- BufferRefs can expire when ring slots are recycled; explicit pinning/leases are not implemented.
- Deterministic recording/replay and subscriptions are not included.
- Provider containment, restart backoff, and stale-state invalidation need expansion.
- Control Authority Leases, decision lineage, safe relinquish, and Manager-owned shutdown now have an implemented guarded path, but still require broader fault-injection and long-duration qualification.
- The Test Agent demonstrates discovery, permission-gated action, and browser-based observation/development controls; it is not a hardened autonomous production agent or operator console.

## reBot arm prototype limits

- Integrated MIT `ONE_SHOT` and MIT `HOLD_LB` are the only arm motion profiles currently marked usable.
- POS_VEL `ONE_SHOT` is limited to paths at or below 20 cm with no payload or high external load. Greater distance or load is not considered stable.
- POS_VEL `HOLD_LB` is experimental and unstable and is excluded from Manager capability discovery.
- Arm POS_TOR `ONE_SHOT`/CONTACT_WORK is experimental and unstable and is excluded from Manager capability discovery.
- Obstacle-route planning is not implemented. Semantic point-cloud objects can be staged, but the current preview is diagnostic rather than a route-search authority.
- The guarded Manager revision exposes control-authority leases and reviewed authorization decisions. The physical implementation remains prototype-grade and is not safety certified.
- USB/serial transport timeouts and mode-confirmation faults remain physical qualification risks.

## FoundationPose object-pose limits

- Published Base and Gripper transforms are camera-relative measurements, not world-frame authority.
- Tracking quality depends heavily on the initial mask; fragmented masks, unrelated pixels, partial occlusion, and symmetric CAD geometry can produce unstable or plausible-but-wrong poses.
- The Gripper target remained less stable than the Base in the observed test setup. Increasing the requested rate to 60 Hz did not correct the pose behavior.
- The Provider serializes expensive GPU work, so requested rates are upper bounds rather than guaranteed throughput.
- OpenAI boxes and SAM2 masks are initialization aids, not safety-rated perception. Operator review remains required.
- Color refinements are empirical for the present lighting and materials. Lab distance 30 plus radius-2 dilation worked for the Base; median RGB with 10% drift plus radius-2 dilation worked for the neon-green Gripper root.
- A bounded stationary camera/world-to-arm alignment Skill is implemented for
  the current workcell. Side-mounted bases are represented by their transform
  instead of being rotated to look upright. Uncertainty qualification and
  broader hardware/camera portability remain open.

## Highest-priority milestone

Add deterministic synchronized recording and replay for RGB, aligned/native depth, IR, accelerometer, gyroscope, calibration/transform revisions, and all timestamp domains.

Use identical recordings to compare:

1. the current Python inertial-first ESKF;
2. a native Basalt adapter;
3. an OpenVINS/MSCKF evaluation build with licensing isolated and reviewed.

Measure orientation latency, absolute/relative trajectory error, stationary drift, visual-outage drift, reacquisition discontinuity, RGB-D versus IR/depth correction quality, CPU, memory, deployment complexity, and licensing suitability.

## Calibration roadmap

- Measure camera-to-IMU time offset and uncertainty.
- Validate camera/IMU extrinsics against motion data.
- Characterize temperature-dependent IMU bias if material.
- Add a full cross-axis accelerometer model only if diagonal scale/offset residuals are insufficient.

## Safety roadmap

Before expanding autonomous robot motion:

- broaden fault-injection qualification for fenced Control Authority Leases;
- qualify safe relinquish on expiry, process failure, Manager disconnect, and lost upstream authority;
- complete physical acceptance for every advertised motion profile;
- keep emergency stop independent from software recovery;
- review every hardware-specific Provider separately.
- physically qualify explicit frame and resolved-vector enforcement for
  semantic Cartesian commands across representative mounts.
