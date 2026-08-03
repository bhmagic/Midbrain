# Executive Handover

## Objective

The project is a framework-neutral physical-agent runtime for robots. Persistent **Resource Providers** own hardware, computation, models, data streams, recording, visualization, or control. Finite **Skills** request those capabilities, perform bounded work, return structured results, and finish. The **Resource Provider Manager** is the control plane. The **World State Fabric** is the timestamped state plane.

The current baseline combines head sensing and local space cognition with GUI-assisted CAD-based object-pose tracking, guarded arm control, and one autonomous Agent runtime. It supplies RGB, depth, infrared, point cloud, calibrated IMU, transforms, an inertial-first local VIO reference backend, Base/Gripper pose measurements, initialization/reset orchestration, regular/developer Agent views, and diagnostic GUIs.

## Latest baseline

The current integrated workspace checkpoint includes:

- Manager/Fabric v0.3.0
- Femto Bolt Provider package v0.4.0
- Local VIO Provider package v0.4.0
- Test Agent v0.4.2
- FoundationPose finite Skill v0.1.0 with Provider v0.4.0 compatibility route
- reBot Arm DM Basic Provider v0.1.20
- reBot Arm Integrated Provider v0.8.1

The spatial-cognition v0.3.10 hardware result remains the best recorded practical localization baseline. The current workspace is not yet a production-qualified localization or autonomous-control stack because formal trajectory measurements, deterministic replay, camera/IMU time-offset estimation, long-outage drift tests, mature-backend comparison, remote command security, and physical safety qualification remain outstanding.

Spatial convention V2 defines every new VIO epoch as +X front, +Y left, and +Z
up opposite gravity. Ordinary 3D language uses that world convention and is
resolved into the arm base only through current transform evidence. Native
camera optical geometry remains X-right, Y-down, Z-forward and uses explicit
`camera_system_x/y/z` component names.

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
- reBot Arm Integrated 0.8.1 with a hardware-test GUI, Cartesian IK target staging, latched gripper control, and Manager capability readiness.
- Reviewed arm discovery labels: MIT one-shot and continuous usable; unloaded POS_VEL one-shot limited to paths ≤20 cm; continuous POS_VEL and arm POS_TOR one-shot hidden as experimental/unstable.

- One autonomous OpenAI Agents SDK driver shared by the regular and developer
  pages through the canonical `/api/streaming-runs` contract.
- Backend-owned replayable SSE events, expandable public reasoning summaries,
  and approval pause/resume without exposing raw chain-of-thought or tool
  payloads.
- One optional user image per prompt, separate retained robot-camera evidence,
  interactive SVG point/box annotations, and flattened copy/download output.
- Manager-boot-scoped shared chat plus a bounded robot-local SQLite run journal
  and two-level read-only event viewer.
- Gemini Robotics-ER 2.0 as the default Robotics-ER visual backend, with
  bounded transient VLM retry and first-camera-frame recovery.

## The main architectural correction

The original requirement was VR-style inertial-first tracking: gyro and accelerometer propagate state continuously, while visual observations correct drift. The first Local VIO implementation in v0.3.1 instead used RGB-D PnP as pose authority and added IMU assistance around it. That visual-first detour caused poor fast-rotation and dim-room behavior and made gravity correction compete with visual odometry.

Beginning with Local VIO v0.2.0 in Space Cognition v0.3.8, the core was replaced with the intended propagation/update architecture. Every ordered IMU sample propagates orientation, position, velocity, gyro bias, accelerometer bias, and covariance. RGB-D and IR/depth estimates are gated correction measurements rather than pose replacements.

## Safety boundary

Perception results do not authorize robot motion. Motion inhibit is used during VIO initialization, but neither the accelerometer calibration GUI nor the FoundationPose tracking GUI immobilizes a robot. The reBot arm prototype retains authorization decisions, Basic lease fencing, Integrated validation, gravity-float, safe-home behavior, and controller completion evidence. The Agent may execute only the exact reviewed relative-motion preview after its session policy or SDK approval resolves; the manual hardware-test GUI retains a separate local Engage/Xbox gate. Neither path is safety-certified. Any assembled robot must be physically secured or separately disabled before calibration, mask initialization, object-pose capture, or sensor-origin work.
