# Architecture and Data Flow

## Control plane: Resource Provider Manager

The Manager discovers Providers from manifests and configuration, starts and stops them, forwards generic Provider requests, tracks health and heartbeat expiry, exposes a capability catalog, resolves declared Provider dependencies, and owns motion-inhibit coordination. A `HOT` request for one Provider starts and activates its transitive dependencies in dependency order and deduplicates shared dependencies. The Agent and Skills request the task-facing Provider or capability once; they do not replay the dependency graph as a sequence of model decisions. Fallback selection, predictive residency policy, and fully fenced Control Authority Leases remain longer-term work.

Provider residency is:

- `COLD`: process absent.
- `WARM`: process running with protected/high-cost resources mostly released.
- `HOT`: required initialization completed and declared capabilities may be ready.

Readiness remains capability-specific. A Provider can be HOT while an optional stream is unavailable or degraded.

If a Manager-owned Provider process exits while a `HOT` transition is
converging, Manager fails immediately with the stable
`PROVIDER_PROCESS_EXITED` code, provider ID, PID, process state, and exit
status. A still-running Provider retains the bounded 15-second readiness
window and fails as `PROVIDER_HOT_TIMEOUT`. Skills preserve these bounded JSON
diagnostics in their own domain error instead of reducing them to an opaque
HTTP 500.

## Arm controller roles

Basic is the sole hardware transport and final limit, fencing, deadline, and
gravity-float authority. The Integrated Provider owns collision-aware
free-space motion and prohibits deliberate sustained contact. The independent
Contact Work Provider owns deliberate contact, has its own Python environment
and Basic arm-group lease, and never calls or imports Integrated.

The independent Grip Provider owns only the gripper actuator group, its 50 Hz
background position/effort, MIT-position, or MIT-float transition, whole-active-joint new-grip
temperature gate, contact inference, and runtime attachment identity. It
declares Basic and Contact as Manager-resolved dependencies. A finite Grip Skill
requests the task-facing Grip Provider once; it does not reproduce that
dependency graph. While carrying, Contact owns every arm move and holds the
last arm target, Grip holds the gripper target, and all active joints remain
`POSITION_EFFORT_LIMITED` even during idle.

A finite task-specific Contact Work Skill signs one complete Cartesian
pose/wrench/timing plan. Each move explicitly selects a one-shot endpoint or a
Contact-owned Cartesian segment. Contact advances a segment through sequential
full-pose IK knots at Basic's advertised internal control rate (currently
50 Hz), enforces a 0.1 m/s Cartesian command-speed ceiling in addition to
Basic's joint limits, maps the full six-component acting-point wrench through
the geometric Jacobian transpose, and holds the final Basic
`POSITION_EFFORT_LIMITED` endpoint until the next move or relax. The first
endpoint establishes Basic's arm-group mode guard; subsequent segments retain
that guard and chain from the previous commanded setpoint so measured tracking
lag cannot create an unlocked or lowered-setpoint gap. The solver minimizes a weighted
full-pose residual and keeps declared joint locks as hard constraints. The
first development Skill is non-clamping slicing and
always sets rotational wrench components to zero. Its mounted-effector
blade-use profile may own locks needed for a particular use orientation. It
delegates only its initial free-space blade alignment to Integrated, waits for
Integrated to finish in gravity float, and then submits engage, slice, and
retract directly to Contact. Provider support for rotational wrench components
remains available for separately qualified future Skills.

The Contact Provider does not plan collisions or decide task success. It
publishes measured joints and command disposition and permits signed endpoint
replacement without a Provider-side arrival gate. Shared finite-Skill runtime
waits for trajectory completion and then applies the signed physical-stage
dwell before submitting the next step. Contact returns to verified
gravity float after explicit cleanup, inactivity timeout, authorization
expiry, fault, lease loss, motion inhibit, or shutdown.

Contact target position mode is explicit. Absolute moves bind a root-frame
endpoint. Measured-start-relative moves bind a root-axis displacement and
resolve it from fresh controlled-effector FK at acceptance. Slicing uses the
relative form for extraction so preceding unreachable-target residual does not
rotate or lengthen the requested outward displacement.

The generic `Action: grip` Skill uses Contact's published measured acting-frame
pose to acquire a zero-displacement current-pose hold before the gripper closes.
`Action: scrap grip` retains the stable `grip_object` tool identity and composes
concurrent Grip-owned functional opening with Slicing-style Integrated
rotation-only alignment, verified gripper approach readiness and WARM/lease handoff,
Contact absolute approach/table motion, relative insertion, stable-contact
grip dwell, then carry confirmation. When the Agent specifies an offset from
current IK/current effector, Scrap Grip captures measured Integrated FK itself;
it does not use VLM effector localization as a substitute origin. `Action: lay
gripped object flat` declares the same Integrated rotation handoff as a
confirmed-carry exception, then uses Contact absolute
placement and relative retreat. Other carried movement remains Contact-only.
`Action: let go` opens under position/effort, enters gripper MIT
float after measured opening, then permits Contact relaxation.

## State plane: World State Fabric

Providers publish timestamped observations containing schema, source identity, boot and instance IDs, monotonic sequence, timestamps, validity, coordinate frame, calibration revision, and optional freshness. The Fabric supplies stream discovery, timestamp-nearest multi-stream queries, and a native transform graph.

Observation sequence must remain monotonic for a Provider boot. A VIO reset changes the session epoch and world-frame identity but must not reset the Provider observation sequence. An earlier bug violated this rule and caused all post-reset observations to be rejected.

## Large payload transport

RGB, depth, IR, aligned depth, and point cloud payloads stay in Windows named shared memory. Fabric observations contain BufferRefs with mapping, slot, generation, offset, length, format, shape, and timestamps. Consumers must treat references as disposable because a ring-buffer slot can be recycled. A recycled BufferRef is a dropped frame, not a permanent map failure.

The provider-neutral `contracts/python` client is the shared consumption
boundary for the rebuilt FoundationPose flow. It opens only the mapping and
exact offsets named by Fabric-issued references and rechecks the committed
generation before and after each copy. FoundationPose, SAM2, and
`locate_arm_base` each install it into a different local `.venv`; the Skill
does not install or import either Provider implementation.

A future milestone should add explicit BufferRef leases or pinning for consumers that require longer retention.

## Transform graph

The Fabric owns the framework-neutral timestamped transform graph. Current relevant frames include:

- Camera optical frames.
- Depth and IR frames.
- Body base.
- Session-specific Local VIO world frame: `local_vio/<session_epoch>`.
- Reviewed active arm-base frame, for example `rebot_arm_base`.

Static camera/IMU extrinsics come from the camera Provider. Dynamic Local VIO body transforms come from the VIO Provider. A forced reinitialization creates a new world frame and invalidates old map points because their coordinates belong to the previous epoch.

The finite `locate_arm_base` Skill produces one reviewed candidate for a
world-to-arm-base transform. FoundationPose itself produces only a
camera-relative centered-mesh measurement and never claims a world-frame or
robot-semantic transform. The Skill owns CAD/reference assets, segmentation
prompt policy, bounded CAD-axis ambiguity resolution, composition, and
candidate evidence. Manager alone activates the reviewed transform.
The active mounted-effector profile owns replaceable effector landmark
semantics, and Basic owns timestamped joint state and FK; Locate Arm Base only
combines those published inputs with its bounded workflow policy.

## FoundationPose object-pose flow

1. Manager, Fabric, the aligned RGB-D camera, a timestamped world axis, the
   SAM2 Provider, and the FoundationPose Provider are available.
2. `locate_arm_base` copies one synchronized RGB-D bundle and queries
   `world_from_camera` at the exact capture timestamp.
3. The Skill independently gives its base CAD atlas, full-arm no-effector
   reference, and current RGB image to the configured number of VLM calls. Each
   call returns one positive base-geometry point, one tight box, and one
   negative point on the excluded support. The initial qualification
   configuration uses two Gemini Robotics-ER 2.0 calls. Agent-owned runs pass
   the operator's run-scoped visual-model selection into the Skill; standalone
   developer runs use the same ER 2.0 default. The Skill retains prompt,
   structured-output, and evidence ownership in its separate environment.
4. The Skill invokes `perception.image.sam2.segment` once for every independent
   VLM prompt. The SAM2 Provider returns one scored mask artifact per call
   without owning robot semantics, ensemble review, or voting policy.
5. The Skill renders and retains every successfully acquired mask without a
   post-SAM2 VLM review. A coded pixel vote retains pixels present in at least
   half of all acquired masks, using `ceil(acquired_mask_count / 2)`, and the Skill dilates that one
   voted mask exactly once.
6. The Skill independently invokes `perception.known_object_pose.estimate` the
   configured number of times with that one voted and dilated mask, renders every
   returned pose as projected CAD on the current RGB image, and asks the VLM
   to select the best geometric fit. Before selection, it applies one fixed
   local-X 180-degree half-turn to the known upside-down pose family and excludes
   any fit that still lacks the configured semantic arm-base +Z/world +Z
   alignment, while retaining every raw render as evidence. Mask-attempt and fit
   counts are independent per-run Skill developer controls rather than Provider
   contracts. A below-threshold two-call disagreement permits one final VLM tie
   break; a consensus selection requires a unique candidate with at least two
   above-floor votes.
   FoundationPose's raw ranking score is retained as audit provenance, is
   withheld from the fit-selection VLM, and is not treated as calibrated
   confidence or a selection threshold.
7. The Skill follows the active mounted-effector profile and asks one VLM call
   to locate one or more of its named visual landmarks in the current RGB. One
   recognized point is sufficient; VLM confidence is audit-only. The Skill
   queries Basic's controlled-effector FK at the capture timestamp, projects
   the profiled landmark under only the arm profile's 0/90/180/270-degree
   local-Z candidates, and chooses the smallest coded image-space error. If the
   effector is not identified, the Skill returns an actionable Agent retry that
   requires a rough world-frame arm-base +X vector; the explicit rerun selects
   the nearest bounded candidate without another effector VLM call. Separate
   Internal artifacts preserve the recognized/projected points and every raw
   fit. Agent evidence is consolidated into an all-mask multicolor raster, an
   optional raw-RGB VLM-point card, and one mask-plus-CAD result whose final
   semantic axes are vector layers. The pre-rotation axes are retained as
   vector layers that default to hidden.
8. The Skill queries the same capture-time transform again, requires its frame,
   epoch, and historical matrix to remain identical, composes in the exact active
   `local_vio/<session_epoch>` frame, and emits an immutable,
   non-motion-usable review candidate. Manager verifies exact review,
   provenance, spatial conventions, and current camera identity before
   publishing `transform.world.arm_base`.

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
normalized point, box, or vector annotations. Individual layers may default to
hidden while remaining operator-toggleable. The SDK-specific run stream is projected
onto the versioned Midbrain event contract, and the browser renders those
records as an interactive SVG overlay. The raster and annotation records remain
separate authoritative artifacts; browser copy/download is a convenience
flattening step. Channel applicability is explicit so a future synchronized
RGB-D producer can add depth without showing RGB-only annotations on that
channel. The initial pointing and general scene Skills publish RGB only.

## Agent user-image input

An operator-selected image follows a separate SDK-neutral attachment route.
The browser uploads one validated still image into a bounded Midbrain store and
receives an opaque attachment ID. Developer Agent requests contain
that ID rather than image bytes. Immediately before Agent execution, the active
runtime adapter resolves it into the model's native text-plus-image input
shape. Text-only runs preserve the legacy string path.

User attachments are conversational context, not Fabric observations. They
cannot satisfy live-camera readiness, depth, calibration, spatial-frame,
freshness, or physical-authorization requirements. Robotics-ER Skills continue
to obtain exact evidence from the robot camera through their existing route.

## Agent conversation projection

The Developer Agent is the browser projection over one autonomous
`PrototypeAgentDriver`, one process-scoped model session, and one
run/approval/streaming implementation. It submits only to the canonical
`/api/streaming-runs` contract; there is no synchronous execution route or
developer execution alias. Developer diagnostics do not change the Agent's
eligible tools, lifecycle policy, retries, or authorization behavior.

Hosted Agent model transport is selected at the model-adapter boundary. A
Gemini model uses Google's OpenAI-compatible chat-completions endpoint and its
Gemini credential; GPT models retain native OpenAI Agents SDK resolution. The
browser receives model-specific reasoning choices, and the server validates
the pair again before a run. This transport choice does not change Skill,
Limited Graph, Manager, Provider, Fabric, authorization, or controller duties.

The canonical browser path remains streaming for every model family. A
`gpt-*` selection retains the original OpenAI Responses surface, including
deferred Skill tools and native hosted `ToolSearchTool`. Every non-`gpt-*`
selection receives the client-executed compatibility surface: an ordinary
`tool_search` FunctionTool exposes the exact deferred names and descriptions,
returns the selected original full definitions in a completed client search
envelope, and makes them callable on the following model turn. Limited Graph
and other nondeferred tools remain immediate. This adds no discovery policy or
registry and changes neither the normalized event stream nor physical
authority. A Chat Completions transport still requires the documented second
model request and does not claim native Responses item types, hosted
same-response continuation, or Responses cache semantics.

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
the exact `MOUNTED_CANONICAL_CAMERA_CALIBRATION_GATED_V2` or
`MOUNTED_CANONICAL_CAMERA_CALIBRATION_GATED_V3` policy, a temporary Local VIO
`DEGRADED` state does not invalidate the stationary-camera calibration and
does not trigger the upright-mount fallback. Locate Arm Base emits V3; V2
remains readable for existing activations, and unknown future versions fail
closed. The host binds the activation and transform revision into the preview
and verifies the same identity again at commit.

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

### Semantic evidence is not an action gate

Object roles such as `WORK_OBJECT`, `KEEP_OUT`, or a caller-defined category
describe scene evidence and planning intent. They do not authorize contact,
select a controller, or define a mutually exclusive Agent action class. The
reference Agent therefore does not narrow the general tool surface merely
because a prompt mentions a work object, its AABB, a named corner, an obstacle,
or a compound motion/contact sequence. A deterministic narrow route is valid
only when the requested finite operation itself is unambiguous; it must not
classify a longer request from one noun or its first action and hide the
remaining eligible Skills.

Planning geometry and control authority remain separate. Integrated consumes
fresh semantic-scene geometry for free-space collision checks. Contact and the
Grip Skills do not receive that collider set: the task-specific Contact Skill
signs the deliberate-contact plan and Contact enforces its own controller
boundary. Read-only coordinate Skills may use an object ID, AABB corner, or
typed frame to derive a target, but their result grants neither motion nor
contact authority. The Agent copies their typed outputs unchanged into the
matching downstream fields.

This separation is also the compatibility path for multiple arms and multiple
object taxonomies. The Agent selects a declared finite capability and semantic
arguments; it does not choose a physical arm instance from prompt wording.
Manager and the assembly/resource binding resolve the compatible Provider and
retain explicit resource identity. Adding another arm, effector, or object
category therefore extends manifests, profiles, bindings, and planning
evidence rather than adding global prompt gates or one-arm conditionals.

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
resume, approval, or deletion path. The Developer Agent links to the viewer, and
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
