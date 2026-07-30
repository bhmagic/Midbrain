# Architecture and Data Flow

## Control plane: Resource Provider Manager

The Manager discovers Providers from manifests and configuration, starts and stops them, forwards generic Provider requests, tracks health and heartbeat expiry, exposes a capability catalog, and owns motion-inhibit coordination. The long-term design also assigns it dependency resolution, fallback, residency policy, and fenced Control Authority Leases.

Provider residency is:

- `COLD`: process absent.
- `WARM`: process running with protected/high-cost resources mostly released.
- `HOT`: required initialization completed and declared capabilities may be ready.

Readiness remains capability-specific. A Provider can be HOT while an optional stream is unavailable or degraded.

## State plane: World State Fabric

Providers publish timestamped observations containing schema, source identity, boot and instance IDs, monotonic sequence, timestamps, validity, coordinate frame, calibration revision, and optional freshness. The Fabric supplies stream discovery, timestamp-nearest multi-stream queries, and a native transform graph.

Observation sequence must remain monotonic for a Provider boot. A VIO reset changes the session epoch and world-frame identity but must not reset the Provider observation sequence. An earlier bug violated this rule and caused all post-reset observations to be rejected.

## Large payload transport

RGB, depth, IR, aligned depth, and point cloud payloads stay in Windows named shared memory. Fabric observations contain BufferRefs with mapping, slot, generation, offset, length, format, shape, and timestamps. Consumers must treat references as disposable because a ring-buffer slot can be recycled. A recycled BufferRef is a dropped frame, not a permanent map failure.

A future milestone should add explicit BufferRef leases or pinning for consumers that require longer retention.

## Transform graph

The Fabric owns the framework-neutral timestamped transform graph. Current relevant frames include:

- Camera optical frames.
- Depth and IR frames.
- Body base.
- Session-specific Local VIO world frame: `local_vio/<session_epoch>`.
- Observed Base frame: `observed_object/rebot_b601_dm/base`.
- Observed Gripper frame: `observed_object/rebot_b601_dm/gripper_slider_support`.

Static camera/IMU extrinsics come from the camera Provider. Dynamic Local VIO body transforms come from the VIO Provider. A forced reinitialization creates a new world frame and invalidates old map points because their coordinates belong to the previous epoch.

The finite FoundationPose Object Localization Skill produces camera-relative
Base and Gripper measurements for a bounded parent operation. It does not claim
a world-frame transform. Stationary Alignment owns sampling, transient
rejection, CAD-symmetry resolution, camera-to-world solving, and publication
under its own authority. The legacy Provider can still publish equivalent
dynamic measurement edges for compatibility comparisons.

## FoundationPose object-pose flow

1. Manager, Fabric, the RGB-D camera Provider, Local VIO, and the parent
   Stationary Alignment Skill are available.
2. The parent holds motion inhibit and captures synchronized RGB-D evidence
   while the arm and camera remain stationary.
3. Reviewed regions are converted into explicit Base/Gripper masks.
4. The parent invokes `foundation_pose_object_localization` for one bounded
   attempt and passes the current VIO epoch with its evidence.
5. The nested Skill loads the FoundationPose runtime, registers the prepared
   CAD asset, and returns camera-relative samples.
6. The nested Skill closes estimator sessions, prepared-model caches, model
   objects, and the CUDA raster context before returning.
7. Stationary Alignment validates and aggregates the samples, then publishes
   only its own reviewed alignment result.

## Startup data flow

1. `Start Midbrain.cmd` starts Manager, Fabric, and the idle Agent UI service,
   then opens the Manager-hosted main portal.
2. Providers remain `COLD`; the portal shows their configured identity,
   liveness, readiness, and observation links without activating them.
3. The operator enters a guarded development flow or asks an Agent to perform a
   task.
4. The Agent or bounded Skill inspects current runtime state and requests
   approval for required Provider activations.
5. For spatial initialization, the formal Initialize / Re-establish Space
   Cognition Skill selects the camera, depth, IMU, and VIO Providers. A
   deliberate re-origin is approval-gated and revokes active workcell
   calibration before changing epoch.
6. The Skill acquires motion inhibit.
7. It waits until Local VIO reports `motion_inhibited: true`.
8. Local VIO creates a new session epoch.
9. The initializer selects the newest required accelerometer and gyro samples
   in their common IMU time domain.
10. It estimates gravity direction, gyro zero-rate bias, and residual noise.
11. It initializes the inertial state and publishes body pose and transforms.
12. Motion inhibit is released after VIO reaches a usable tracking state.
13. Observation pages and authorized consumers can then inspect or use the
    published state.

## Runtime data flow

- Ordered IMU samples continuously propagate the inertial state.
- High-rate predicted poses may be published between camera corrections.
- RGB plus aligned depth is the primary metric correction source.
- Synchronized IR plus native depth is an optional low-light correction source.
- Gravity leveling uses quiet IMU windows to constrain roll/pitch without changing yaw or translation.
- The GUI consumes Fabric status and BufferRefs, renders an orthographic world view, and reports each estimator stage independently.

## Reset data flow

1. Suspend new point-cloud insertion.
2. Keep the previous map visible while reset begins.
3. Acquire or confirm motion inhibit.
4. Create a new Local VIO session epoch without resetting observation sequence.
5. Reinitialize the inertial state.
6. Clear old-epoch points only after the new epoch is accepted.
7. Reset camera frame cursor and reopen shared-memory readers.
8. Resume point accumulation when a body pose and synchronized RGB-D bundle are available in the new epoch.
