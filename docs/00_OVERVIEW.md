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
- FoundationPose finite Skill v0.1.0 with Provider v0.3.0 compatibility route

The operator reported v0.3.10 as performing well after the final startup correction. This is the best current practical baseline. It is not yet a production-qualified localization stack because formal trajectory measurements, deterministic replay, camera/IMU time-offset estimation, long-outage drift tests, and comparison with a mature native backend remain outstanding.

## What is working

- Manager-hosted Midbrain main GUI as the primary portal for system status,
  Provider and Skill observation, guarded development links, regular/developer
  Agent access, and whole-workspace shutdown.
- Windows Manager and Fabric build and run.
- Femto Bolt RGB, native depth, aligned depth, IR, XYZ point cloud, accelerometer, gyroscope, calibration, identity, and synchronized bundle publication.
- Large camera payloads use Windows named shared memory; Fabric carries generation-checked BufferRefs.
- Device-bound six-position accelerometer calibration GUI with atomic write, backup, and live Provider reload.
- Native timestamped transform graph with session epochs.
- Discoverable Initialize / Re-establish Space Cognition Skill with
  motion-inhibit handshake and approval-gated epoch reset.
- Forced VIO reinitialization revokes active workcell calibration, creates a
  new coordinate epoch, clears epoch-bound mapping, and resumes capture.
- Orthographic isometric point-cloud display with world-down arrow and camera frustum.
- Inertial-first 15-state error-state filter with RGB-D corrections and optional synchronized IR/depth fallback.
- Tuned quiet-IMU gravity leveling shown independently as OFF, READY, or ACTIVE.
- Sample-rate-independent startup initialization verified in regression at 50 Hz.
- Finite FoundationPose object localization for Base and Gripper CAD targets,
  with explicit session, model, raster-context, and CUDA-cache release.
- Camera-relative Base and Gripper transforms published into the Fabric for consumption by other Skills and Agents.
- Reviewed initialization from OpenAI visual boxes and positive points, cropped SAM2 masks, target-specific color refinement, and prepared-asset caching.
- reBot Arm DM Basic 0.1.20 with seven-joint feedback/control, fenced operational leases, gravity-float, safe-home, and tool-payload gravity compensation.
- reBot Arm Integrated 0.7.0 with a hardware-test GUI, Cartesian IK target staging, latched gripper control, and Manager capability readiness.
- Reviewed arm discovery labels: MIT one-shot and continuous usable; unloaded POS_VEL one-shot limited to paths ≤20 cm; continuous POS_VEL and arm POS_TOR one-shot hidden as experimental/unstable.

## The main architectural correction

The original requirement was VR-style inertial-first tracking: gyro and accelerometer propagate state continuously, while visual observations correct drift. The first Local VIO implementation in v0.3.1 instead used RGB-D PnP as pose authority and added IMU assistance around it. That visual-first detour caused poor fast-rotation and dim-room behavior and made gravity correction compete with visual odometry.

Beginning with Local VIO v0.2.0 in Space Cognition v0.3.8, the core was replaced with the intended propagation/update architecture. Every ordered IMU sample propagates orientation, position, velocity, gyro bias, accelerometer bias, and covariance. RGB-D and IR/depth estimates are gated correction measurements rather than pose replacements.

## Safety boundary

The perception milestone does not authorize robot motion. Motion inhibit is used during VIO initialization, but neither the accelerometer calibration GUI nor the FoundationPose tracking GUI immobilizes a robot. The reBot arm prototype has its own explicit operator gates, Basic lease fencing, gravity-float, and safe-home behavior. Upstream target staging is not autonomous physical authority; Integrated still requires local Engage + Xbox LB. Any assembled robot must be physically secured or separately disabled before calibration, mask initialization, object-pose capture, or sensor-origin work.
