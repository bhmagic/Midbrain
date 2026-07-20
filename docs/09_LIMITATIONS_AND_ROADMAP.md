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
