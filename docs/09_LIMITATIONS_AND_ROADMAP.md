# Limitations and Roadmap

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
- Control Authority Leases and safe relinquish remain contract/design work, not a verified robot-motion runtime.
- The Test Agent is a diagnostic mockup, not a hardened operator console.

## FoundationPose object-pose limits

- Published Base and Gripper transforms are camera-relative measurements, not world-frame authority.
- Tracking quality depends heavily on the initial mask; fragmented masks, unrelated pixels, partial occlusion, and symmetric CAD geometry can produce unstable or plausible-but-wrong poses.
- The Gripper target remained less stable than the Base in the observed test setup. Increasing the requested rate to 60 Hz did not correct the pose behavior.
- The Provider serializes expensive GPU work, so requested rates are upper bounds rather than guaranteed throughput.
- OpenAI boxes and SAM2 masks are initialization aids, not safety-rated perception. Operator review remains required.
- Color refinements are empirical for the present lighting and materials. Lab distance 30 plus radius-2 dilation worked for the Base; median RGB with 10% drift plus radius-2 dilation worked for the neon-green Gripper root.
- A bounded camera-to-world alignment Skill is not yet implemented. It should aggregate stationary measurements, reject transients, resolve symmetry using robot geometry or joint context, estimate uncertainty, and publish a separately authoritative transform.

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

Before enabling robot motion:

- implement fenced Control Authority Leases;
- define safe relinquish on expiry, process failure, and Manager disconnect;
- test control behavior in simulation;
- keep emergency stop independent from software recovery;
- review every hardware-specific Provider separately.
