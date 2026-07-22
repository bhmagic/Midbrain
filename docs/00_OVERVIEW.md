# Executive Handover

## Objective

The project is a framework-neutral physical-agent runtime for robots. Persistent **Resource Providers** own hardware, computation, models, data streams, recording, visualization, or control. Finite **Skills** request those capabilities, perform bounded work, return structured results, and finish. The **Resource Provider Manager** is the control plane. The **World State Fabric** is the timestamped state plane.

The current baseline combines head sensing and local space cognition with GUI-assisted CAD-based object-pose tracking. It supplies RGB, depth, infrared, point cloud, calibrated IMU, transforms, an inertial-first local VIO reference backend, Base/Gripper pose measurements, initialization/reset orchestration, and diagnostic GUIs.

## Latest baseline

The latest integrated release is **Space Cognition v0.3.10**:

- Manager/Fabric v0.3.0
- Femto Bolt Provider v0.3.1
- Local VIO Provider v0.2.2
- Test Agent GUI v0.2.9
- Contracts v0.3.8
- FoundationPose Provider v0.3.0

The operator reported v0.3.10 as performing well after the final startup correction. This is the best current practical baseline. It is not yet a production-qualified localization stack because formal trajectory measurements, deterministic replay, camera/IMU time-offset estimation, long-outage drift tests, and comparison with a mature native backend remain outstanding.

## What is working

- Windows Manager and Fabric build and run.
- Femto Bolt RGB, native depth, aligned depth, IR, XYZ point cloud, accelerometer, gyroscope, calibration, identity, and synchronized bundle publication.
- Large camera payloads use Windows named shared memory; Fabric carries generation-checked BufferRefs.
- Device-bound six-position accelerometer calibration GUI with atomic write, backup, and live Provider reload.
- Native timestamped transform graph with session epochs.
- Initialize Space Cognition Skill with motion-inhibit handshake.
- Forced VIO reinitialization creates a new coordinate epoch and resumes mapping.
- Orthographic isometric point-cloud display with world-down arrow and camera frustum.
- Inertial-first 15-state error-state filter with RGB-D corrections and optional synchronized IR/depth fallback.
- Tuned quiet-IMU gravity leveling shown independently as OFF, READY, or ACTIVE.
- Sample-rate-independent startup initialization verified in regression at 50 Hz.
- Manager-discoverable FoundationPose tracking for independent Base and Gripper CAD targets.
- Camera-relative Base and Gripper transforms published into the Fabric for consumption by other Skills and Agents.
- Reviewed initialization from OpenAI visual boxes and positive points, cropped SAM2 masks, target-specific color refinement, and prepared-asset caching.

## The main architectural correction

The original requirement was VR-style inertial-first tracking: gyro and accelerometer propagate state continuously, while visual observations correct drift. The first Local VIO implementation in v0.3.1 instead used RGB-D PnP as pose authority and added IMU assistance around it. That visual-first detour caused poor fast-rotation and dim-room behavior and made gravity correction compete with visual odometry.

Beginning with Local VIO v0.2.0 in Space Cognition v0.3.8, the core was replaced with the intended propagation/update architecture. Every ordered IMU sample propagates orientation, position, velocity, gyro bias, accelerometer bias, and covariance. RGB-D and IR/depth estimates are gated correction measurements rather than pose replacements.

## Safety boundary

This milestone is perception and state infrastructure. It does not authorize robot motion. Motion inhibit is used during VIO initialization, but neither the accelerometer calibration GUI nor the FoundationPose tracking GUI immobilizes a robot. Any assembled robot must be physically secured or separately disabled before calibration, mask initialization, object-pose capture, or sensor-origin work.
