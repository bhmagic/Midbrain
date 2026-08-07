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

## VLM arm-root translation-refinement flow

The finite
[`refine_arm_root_translation`](../skills/refine-arm-root-translation/SKILL.md)
Skill improves only XYZ translation after another workflow has established a
motion-usable world-to-arm-base transform and trusted rotation. It performs no
robot movement and cannot establish the missing three rotational parameters
from one observed point.

1. The Reference Agent discovers the Skill from its manifest. Its generic
   external-Skill host loads the Skill-owned hardware bridge and launches the
   numerical runtime from the Skill's private Python environment.
2. The host verifies the active alignment, camera/VIO/arm identities, selected
   effector profile, and a current local arm FK path before starting VLM work.
3. For each requested sample, it captures one fresh RGB/registered-depth pair,
   brackets the immutable capture window with timestamped arm FK, and rejects
   the sample when profile-bounded landmark motion exceeds its limit.
4. The VLM marks the profile-selected physical feature in both RGB and
   registered depth. The current bare-gripper profile uses the mean of the two
   lateral endpoints of the neon-green rail. Invalid exact depth permits at
   most one VLM reselection; coded nearest-pixel repair is not used.
5. The profile rotates its measured rigid landmark offset with timestamped FK.
   For the current gripper, rail center to controller tip is
   `[+0.080, 0, 0]` m in controlled-frame coordinates. The Skill reconstructs
   the controller tip and estimates only the base translation while preserving
   the active rotation byte-for-byte.
6. A caller may request one to five independent samples and an adoption factor
   from zero to one. Multi-sample mode averages raw XYZ corrections and submits
   at most one state update. A sufficiently large raw delta requires a second
   marked-image VLM quality review before it can be accepted.
7. The Skill returns exact visual evidence for the selected landmark plus old
   and proposed base/landmark projections. Accepted updates use Manager's
   expected-revision compare-and-swap route.
8. Manager revalidates active state, identities, arm health, locked rotation,
   and arithmetic consistency; publishes the new transform through Fabric;
   then increments one flat refinement revision and retains a bounded rollback
   journal. It does not duplicate the Skill's perception-quality policy.

An arm-FK or Fabric history gap fails before landmark inference or state
mutation. Do not turn missing timestamp bracketing into an exact transform by
assuming that silence means the arm was stationary. The current investigation
handoff is
[`vio_and_arm_fk_timestamp_anomaly_handoff.md`](../skills/refine-arm-root-translation/references/vio_and_arm_fk_timestamp_anomaly_handoff.md).

## Startup data flow

1. `Start Midbrain.cmd` starts Manager, Fabric, and the idle Agent UI service,
   then opens the Manager-hosted main portal.
2. Providers remain `COLD`; the portal shows their configured identity,
   liveness, readiness, and observation links without activating them.
3. The operator enters a guarded development flow or asks an Agent to perform a
   task.
4. The Agent or bounded Skill inspects current runtime state and requests host
   policy authorization for required Provider activations. Development may
   project an unresolved decision into an approval dialog.
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

## Agent visual evidence

Camera-facing Agent Skills retain the exact frame they analyzed and may return
normalized point or box annotations. The SDK-specific run stream is projected
onto the versioned Midbrain event contract, and the browser renders those
records as an interactive SVG overlay. The raster and annotation records remain
separate authoritative artifacts; browser copy/download is a convenience
flattening step. Channel applicability is explicit so a future synchronized
RGB-D producer can add depth without showing RGB-only annotations on that
channel. The initial pointing and general scene Skills publish RGB only.

## Agent user-image input

An operator-selected image follows a separate SDK-neutral attachment route.
The browser uploads one validated still image into a bounded Midbrain store and
receives an opaque attachment ID. Requests from either Agent view contain
that ID rather than image bytes. Immediately before Agent execution, the active
runtime adapter resolves it into the model's native text-plus-image input
shape. Text-only runs preserve the legacy string path.

User attachments are conversational context, not Fabric observations. They
cannot satisfy live-camera readiness, depth, calibration, spatial-frame,
freshness, or physical-authorization requirements. Robotics-ER Skills continue
to obtain exact evidence from the robot camera through their existing route.

## Agent conversation projection

The regular page and developer view are presentation variants over one
autonomous `PrototypeAgentDriver`, one process-scoped model session, and one
run/approval/streaming implementation. Both pages submit only to the canonical
`/api/streaming-runs` contract; there is no synchronous execution route or
developer execution alias. Developer diagnostics do not change the Agent's
eligible tools, lifecycle policy, retries, or authorization behavior.

The browser groups each backend-owned run into one user/Agent turn. Public
reasoning-summary deltas and sanitized lifecycle events populate an expandable
execution summary, while visual evidence remains attached to the turn that
created it. The projection excludes private chain-of-thought and raw tool
payloads.

The Manager boot UUID parents a robot-local conversation session. The journal
batches the prompt, public answer, model-selection metadata, and normalized
events into SQLite. Both pages hydrate and periodically synchronize from the
same active-session projection, so closing a tab or opening both pages does not
fork or erase the transcript. Attached image bytes, private reasoning, and raw
tool payloads are excluded. The SDK model-session database remains separate.
The journal marks nonresumable prior-process runs interrupted and stays outside
the command and authorization paths. It is durable development diagnostics,
not yet an authenticated field-audit store.

The local developer service supplies that durable observation view at
`/dev/run-journal`. Its bounded GET endpoints return Manager-boot sessions,
their Agent runs, and a selected run's normalized envelopes. The left pane adds
the session parent above the existing run cards. The right pane groups
envelopes behind category and per-event disclosures. It adds no command,
resume, approval, or deletion path. Both Agent views link to the viewer, and
the Manager portal exposes the same loopback surface.
