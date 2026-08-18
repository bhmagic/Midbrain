# Architecture and Data Flow

## Control plane: Resource Provider Manager

The Manager discovers Providers from manifests and configuration, starts and stops them, forwards generic Provider requests, tracks health and heartbeat expiry, exposes a capability catalog, resolves declared Provider dependencies, and owns motion-inhibit coordination. A `HOT` request for one Provider starts and activates its transitive dependencies in dependency order and deduplicates shared dependencies. The Agent and Skills request the task-facing Provider or capability once; they do not replay the dependency graph as a sequence of model decisions. Fallback selection, predictive residency policy, and fully fenced Control Authority Leases remain longer-term work.

Provider residency is:

- `COLD`: process absent.
- `WARM`: process running with protected/high-cost resources mostly released.
- `HOT`: required initialization completed and declared capabilities may be ready.

Readiness remains capability-specific. A Provider can be HOT while an optional stream is unavailable or degraded.

## Arm controller roles

Basic is the sole hardware transport and final limit, fencing, deadline, and
gravity-float authority. The Integrated Provider owns collision-aware
free-space motion and prohibits deliberate sustained contact. The independent
Contact Work Provider owns deliberate contact, has its own Python environment
and Basic arm-group lease, and never calls or imports Integrated.

A finite task-specific Contact Work Skill signs one complete Cartesian
pose/wrench/timing plan. Each move explicitly selects a one-shot endpoint or a
Contact-owned Cartesian segment. Contact advances a segment through sequential
full-pose IK knots at Basic's advertised internal control rate (currently
50 Hz), maps the full six-component acting-point wrench through the geometric
Jacobian transpose, and holds the final Basic `POSITION_EFFORT_LIMITED`
endpoint until the next move or relax. The solver minimizes a weighted
full-pose residual and keeps declared joint locks as hard constraints. The
first development Skill is non-clamping slicing and
always sets rotational wrench components to zero. Its mounted-effector
blade-use profile may own locks needed for a particular use orientation. It
delegates only its initial free-space blade alignment to Integrated, waits for
Integrated to finish in gravity float, and then submits engage, slice, and
retract directly to Contact. Provider support for rotational wrench components
remains available for separately qualified future Skills.

The Contact Provider does not plan collisions or decide task success. It
publishes measured joints and command disposition, immediately replaces the
current endpoint or segment when a new signed-plan step arrives, and returns to verified
gravity float after explicit cleanup, inactivity timeout, authorization
expiry, fault, lease loss, motion inhibit, or shutdown.

Contact target position mode is explicit. Absolute moves bind a root-frame
endpoint. Measured-start-relative moves bind a root-axis displacement and
resolve it from fresh controlled-effector FK at acceptance. Slicing uses the
relative form for extraction so preceding unreachable-target residual does not
rotate or lengthen the requested outward displacement.

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
2. The host reads `robot_arm.assembly_state`, follows Basic's selected
   Provider-owned mounted-effector profile, and binds the run to the assembly
   fingerprint and effector identity/revision. It verifies the active
   alignment, camera/VIO/arm identities, the selected profile's optional
   alignment extension, and a current local arm FK path before starting VLM
   work. An effector without that extension remains a valid assembly but is
   reported as unavailable for this Skill without starting VLM work.
3. For each requested sample, it captures one fresh RGB/registered-depth pair,
   brackets the immutable capture window with timestamped arm FK, and rejects
   the sample when profile-bounded landmark motion exceeds its limit.
4. The VLM marks every profile-selected physical feature in both RGB and
   registered depth. The profile may name one through eight required points.
   All must be detected and registered before their arithmetic 3D mean is
   valid; partial means are rejected. The bare-gripper profile uses the two
   lateral endpoints of the neon-green rail, while the blade profile uses the
   blade-side and rear endpoints of the military-green handle. Invalid exact
   depth permits at most one VLM reselection; coded nearest-pixel repair is not
   used.
5. The profile's Skill-owned namespaced extension rotates its configured rigid
   landmark offset with timestamped FK.
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
4. The Agent invokes the finite Skill first unless the operator explicitly
   requested lifecycle inspection. If the Skill reports a cold dependency,
   the host requests the task-facing Provider `HOT` once and Manager resolves
   declared transitive dependencies. Development may project an unresolved
   lifecycle decision into an approval dialog.
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

For an ordinary requested Integrated relative motion, the reference Agent host
projects nonphysical preview and its exact execution continuation as one
`perform_relative_effector_motion` tool call. Preparation is keyed by the SDK
call ID and retains the opaque preview ID in host memory. A `PREVIEW_READY`
result may proceed only through the existing canonical motion-authorization
policy; execution then reuses the exact pending preview and retains controller
freshness and completion checks. Dependency recovery, calibration, operator
questions, failed previews, and every continuation other than the allowlisted
physical commit return to normal Agent orchestration without being chained.
The lower-level preview and execution tools remain available for explicit
nonphysical preview and compatibility diagnostics.

Each signed Integrated path also binds its Basic execution backend. Omission
selects the 50 Hz `IMPEDANCE` stream. A caller may explicitly select
`POS_SPEED`, which uses the same 50 Hz controller pacing but emits Basic
`POSITION_VELOCITY_LIMITED` targets and derives timing from that mode's
advertised joint limits. The backend is part of the immutable preview digest,
so it cannot be changed between preview and commit.

Spatial direction resolution similarly prioritizes Manager's active reviewed
world-from-arm transform. Under
`MOUNTED_CANONICAL_CAMERA_CALIBRATION_GATED_V2`, a temporary Local VIO
`DEGRADED` state does not invalidate the stationary-camera calibration and
does not trigger the upright-mount fallback. The host binds the activation and
transform revision into the preview and verifies the same identity again at
commit.

When an absolute world XYZ point is already known, the reference Agent instead
uses one `move_effector_to_world_point` call. The host verifies any supplied
world-frame and VIO-epoch identity, resolves the point through the current
reviewed rigid world-to-arm transform, freezes the measured controlled-frame
orientation as a `POSE_6DOF` goal, and uses the same call-scoped signed
Integrated preview/commit boundary. This prevents the model from inspecting
runtime state and manually subtracting coordinates. In a multi-Skill workflow,
a following contact operation may begin only after this free-space operation
reports `physical_motion_completed=true`.

When the point originates from a semantic work-object bound, coordinate
derivation remains a separate read-only finite operation. The Agent first
inspects the Fabric-hosted scene, then calls `derive_fabric_world_point` with
one exact object ID, named visible-surface AABB corner, typed offset vector,
unit, reference axes, and optional inspected scene revision. The Skill reads
one coherent current Fabric snapshot itself, rejects stale evidence, performs
unit conversion and point/vector transform math, and returns
`target_position_world_m`, `target_world_frame_id`, and
`target_session_epoch`. Those three fields cross unchanged into
`move_effector_to_world_point`; the Agent does not add coordinates, subtract
the current effector position, or reinterpret arm-base coordinates as world
coordinates. A later monotonic scene publication does not retroactively
invalidate the fresh snapshot selected at derivation; the optional inspected
revision is provenance rather than an optimistic concurrency lock. Source
expiry and a change of active world-frame authority still fail closed. A
controlled-effector-frame offset uses the latest timestamped
controlled-frame rotation, while the AABB point remains bound to its own
observation timestamp. Both transform paths are retained in derivation
provenance. Coordinate derivation grants no physical or contact authority.

Directions and complete poses use the same separation without overloading the
work-object point operation. `translate_fabric_direction_to_world` accepts one
explicit active-world, arm-base, or controlled-effector direction, applies
rotation only, and returns a normalized `direction_world` with frame, epoch,
calibration, timestamp, and transform-path provenance.
`translate_fabric_pose_to_world` applies the complete rigid transform to one
metric position and XYZW orientation and returns
`target_position_world_m` plus `target_orientation_world_xyzw` under the same
provenance contract. Both are finite read-only operations and grant no motion
authority.

For a mixed-frame contact request, the Agent calls the direction translator
once for each non-world direction and copies `direction_world` unchanged into
the downstream field with the same semantic role. For example, an arm-base
slicing direction crosses into `slicing_direction_world`; it never becomes a
blade direction merely because both are three-element vectors. A world
direction already bound to the active world does not require model-side
transform math. Task-specific motion and contact Skills retain their canonical
world-coordinate contracts and their independent physical authority.

The general reference invariant is one Agent decision per task-facing finite
operation. Such an operation may own several sequential internal API calls
only when their order and continuation are mechanically determined within one
existing responsibility boundary. Each stage remains independently bounded,
validated, observable, and auditable. Crossing into Provider lifecycle
recovery, calibration review, operator input, new observation, replanning, or
uncertain physical-outcome handling ends the compound operation and returns a
typed result to the Agent. A prompt containing several semantic operations
therefore still produces several task-facing calls; the host does not turn a
quest into one opaque transaction.

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

## Bounded multi-Skill graph flow

When the requested workflow contains two or more predetermined eligible finite
Skills, the reference Agent strongly prefers one Limited Graph instead of a
new model decision between each Skill. The graph does not merge those Skills
or transfer their duties. It provides bounded orchestration across their
existing typed boundaries:

1. Manager publishes the regulated complete Provider/capability catalog.
   Arbitrary Provider diagnostics remain available only through an explicit
   top-level detail read.
2. Skill discovery publishes concise inputs, complete result field names, and
   each Skill's declared compact result pointers without importing the Skill.
3. The Agent authors ordered steps, explicit bindings, and any required edge,
   retry, switch, or model-route policy in the concise authoring schema.
4. The Agent host deterministically compiles canonical graph version 1 and
   runs schema, child-eligibility, compact-pointer, reachability, limit, and
   nested-graph preflight before a child starts.
5. Each child executes through the same direct-call host adapter. A child-owned
   Provider continuation may pass through the existing Manager lifecycle
   broker and resume the same child identity; the graph cannot select a
   Provider or carry lifecycle credentials.
6. The host validates the child's complete sanitized result, retains a bounded
   detail copy when configured, and gives the graph only compact fields plus
   an opaque detail reference. Bindings and conditions can read only declared
   compact pointers.
7. Validated visual evidence is projected to the Agent event stream at child
   completion rather than waiting for the whole graph. The graph retains a
   bounded trace and publishes compact terminal state including
   `last_failure` when applicable.
8. The top-level Agent evaluates the graph outcome. Any new semantic decision,
   operator question, unowned recovery, or uncertain physical result starts a
   separate Agent decision rather than an implicit graph continuation.

Authentication, authorization, signed previews, Provider leases, Fabric
ownership, controller validation, and physical-completion truth remain with
their existing owners. See
[Limited Graph Status and Qualification](14_LIMITED_GRAPH_STATUS_AND_QUALIFICATION.md)
for the current acceptance boundary.
