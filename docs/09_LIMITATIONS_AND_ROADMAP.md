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

## Agent autonomy and operator-experience objectives

- [ ] **Faster bounded command completion (next)**: reduce the number of model
  turns needed for common robot commands. First instrument the latency and
  outcome of intent interpretation, runtime inspection, Provider activation,
  finite-Skill planning, preview, authorization, execution, and result
  interpretation. Then give frequent operations, beginning with relative arm
  motion, a deterministic compound-Skill or host-orchestration path after the
  Agent has resolved the user's intent. Preserve SDK-neutral Midbrain events,
  Manager lifecycle policy, controller validation, authority leases, preview
  evidence, and the applicable authorization decision; do not obtain speed by
  bypassing those boundaries. Keep the individual finite Skills available for
  third-party Agent adapters and uncommon recovery paths.
- [ ] **Low-interaction autonomous field missions**: define bounded mission
  policies for environments such as farms and mines so routine authorized
  work can continue without browser dialogs while resource, lease, workspace,
  motion, and stop limits remain enforced locally.
- [ ] **Safe autonomous recovery**: extend retry beyond the completed camera
  and VLM cases only where action identity, completion evidence, idempotency,
  and uncertain-outcome handling can prove that a retry will not duplicate a
  physical action.
- [ ] **Fast visual arm-mount attestation**: avoid waiting for a full arm-base
  pose when the immediate command needs only the known upright installation
  assumption. Use the faster camera pose, a current robot-camera image, and a
  VLM to attest the base +X/+Z relationship. Fall back to the operator question
  whenever the evidence is incomplete or ambiguous.
- [ ] **Contextual development approval cards (near future)**: replace
  transient blocking browser confirmation dialogs with persistent cards on the
  corresponding chat turn. Show the exact action, limits, evidence, and final
  approve/reject decision. This improves development usability and decision
  traceability; it does not reduce model calls and is not the intended field
  authorization workflow.
- [ ] **Active-run steering (optional)**: allow an authenticated operator to
  redirect or refine a running Agent task while preserving the distinction
  between a new instruction and observation-only SSE events.
- [ ] **Real-time gesture control with dead-man behavior**: require a
  continuously renewed enable condition and return to the qualified safe
  hold, float, or stop behavior when gesture tracking, the network, or the
  operator signal is lost.
- [ ] **Secure remote Agent operation (future)**: add authenticated identities,
  roles, transport protection, origin/CSRF enforcement, rate limits, and audit
  identity before any command interface is exposed beyond loopback.
- [ ] **Durable evidence and journal policy (future)**: define retention,
  encryption, deletion, export, redaction, and tamper detection for chat,
  journals, attachments, and visual evidence before using them as field audit
  records.
- [ ] **External Skill docking (optional/parked)**: support externally hosted
  perception or robotics Skills through MCP or another typed adapter boundary.
  Keep local GPU workloads such as FoundationPose as Resource Manager Providers
  when Midbrain must manage their residency, device use, and health.

## Later command-boundary security

- [ ] Before the Agent UI is exposed beyond its default loopback listener, add
  authenticated and role-authorized access to every command-capable route,
  including regular/developer run creation, streaming approval decisions,
  Provider lifecycle operations, calibration, spatial reset, and robot motion.
  Keep observation-only permissions separate from command authority; use TLS
  or an authenticated reverse proxy, short-lived credentials, request/audit
  identity, origin/CSRF protection for browser commands, rate limits, and
  explicit denial by default. The current `127.0.0.1` binding reduces remote
  exposure but is not authentication and does not protect against another
  local process.

## Retry roadmap

- [x] Retry classified transient failures within read-only VLM inference. The
  retry is bounded per backend, preserves attempt provenance, and cannot repeat
  a physical action.
- [x] Add the first domain-specific finite-Skill retry at the RGB capture
  boundary. It names the exact scope, requires fresh evidence, preserves the
  camera binding, and declares that no physical action was submitted.
- [ ] Extend finite-Skill retry classifications only where another narrow,
  bounded, nonphysical or provably idempotent boundary can meet the same
  contract.
- [ ] Add autonomous mission-step retry only after action IDs, controller
  completion evidence, idempotency rules, and uncertain-outcome recovery can
  prove that a retry will not duplicate motion.
- [x] Project capture retry recovery and exhaustion as SDK-neutral Midbrain
  events from the completed Skill result.
- [ ] Add live retry scheduling and attempt-start events if runtime retry
  context is later exposed safely across agent-SDK tool execution.

## Agent image-input roadmap

- [x] Accept one validated user image through a bounded SDK-neutral attachment
  reference on both Agent surfaces.
- [x] Keep user attachments distinct from current robot-camera evidence and
  physical authorization.
- [ ] Define durable attachment retention, deletion, encryption, and chat
  history presentation before treating uploaded images as long-lived records.
- [ ] Add multiple images only with explicit ordering, aggregate size, model
  capability, and history-budget policies.

## Agent conversation-history roadmap

- [x] Render bounded scrollable user/Agent turns on both Agent pages.
- [x] Keep public reasoning summaries and safe lifecycle progress in one
  expandable per-turn execution summary.
- [x] Retain a bounded Agent transcript under the current Manager boot UUID
  and restore it from robot-local SQLite on either Agent page.
- [x] Live-synchronize the regular and developer chat projections while
  preserving ownership of an active SSE stream in the tab that started it.
- [x] Persist the SDK-neutral backend event sequence in a bounded robot-local
  SQLite diagnostic journal and mark nonresumable prior-process runs
  interrupted without changing SSE or physical authority.
- [x] Provide a read-only two-pane run-journal GUI with Manager-boot session
  parents and expandable run cards on the left, two-level normalized event
  detail on the right, and navigation from the portal and both Agent pages.
- [x] Split the developer view into independently scrolling 50/50 diagnostics
  and conversation panes, with individually collapsible diagnostics and exact
  normalized per-turn event envelopes.
- [x] Route the regular page and developer view through one autonomous driver,
  model session, tool policy, pending-approval store, and only the canonical
  `/api/streaming-runs` execution path.
- [ ] Authenticate and harden the robot-local run journal before using it for
  field audit or incident review.
- [ ] Define retention, encryption, redaction, deletion, export, and
  cross-client synchronization policy for that durable journal.

## reBot arm prototype limits

- Integrated MIT `ONE_SHOT` and MIT `HOLD_LB` remain the general direct arm
  motion profiles marked usable.
- Direct POS_VEL `ONE_SHOT` remains limited to paths at or below 20 cm with no
  payload or high external load. The separately authorized transit path can
  describe a longer controller-owned waypoint route, but commit requires a
  fresh semantic scene and exact reviewed authorization.
- POS_VEL `HOLD_LB` is experimental and unstable and is excluded from Manager capability discovery.
- Arm POS_TOR `ONE_SHOT`/CONTACT_WORK is experimental and unstable and is excluded from Manager capability discovery.
- Obstacle-route planning is not implemented. Semantic point-cloud objects can be staged, but the current preview is diagnostic rather than a route-search authority.
- The guarded Manager revision exposes control-authority leases and reviewed authorization decisions. The physical implementation remains prototype-grade and is not safety certified.
- USB/serial transport timeouts and mode-confirmation faults remain physical qualification risks.

### External float/hold/lock control status

Integrated now exposes a provider-local leased idle-profile API independent of
trajectory speed profiles. `GRAVITY_FLOAT`, `COMPLIANT_HOLD`, and
`POSITION_LOCK` capture the measured endpoint, reject a competing holder,
expire automatically, and release to verified Basic gravity float. Compliant
hold accepts only 2x-4x configured Kp; position lock uses a low POS_VEL ceiling.
The Manager capability catalog advertises the contract for outside Skills and
Agents.

- [x] Add provider-local leased float, 2x-4x Kp compliant hold, and POS_VEL
  position-lock semantics.
- [x] Capture the measured endpoint and expose holder, lease ID, expiry,
  keepalive command count, gain/limit, and release reason.
- [x] Keep idle retention independent of motion-planning speed profiles.
- [ ] Bind the idle-profile holder to the Manager's cross-provider authority
  lineage rather than relying only on the exact provider-local lease pair.
- [ ] Resolve the Kd source conflict and validate oscillation, thermal rise,
  external-disturbance recovery, serial loss, and emergency release before
  advertising any high-stiffness profile.

### Perception-gated motion-envelope release TODO

Ordinary Integrated motion now defaults to the Basic Provider's latched
POS_SPEED/POS_VEL endpoint mode. One free-space request may span up to the
1.2 m arm-base ROI; the old 20 cm direct-command split, 0.25 rad/s Integrated
transit ceiling, and independent Cartesian-speed ceiling are retired. The
20 cm limit remains only for `CONTACT_WORK`, where it bounds deliberate
contact rather than ordinary transit.

The official Seeed reBot SDK configuration declares POS_VEL limits of 5.0 rad/s
for the DM-J4340P joints 1-3 and 3.0 rad/s for the DM-J4310 joints 4-6.
The requested Midbrain configuration instead uses 5.0 rad/s for J1-J3 and
10.0 rad/s for J4-J6 and the gripper. The J1-J3 value is below the
DM-J4340P 24 V no-load characteristic speed of 52 rpm (about 5.45 rad/s); the
J4-J6 value is below the DM-J4310 rated characteristic speed of 120 rpm
(about 12.57 rad/s). These are motor-envelope selections, not autonomous
whole-arm qualifications. Requested per-joint speed intent above 10 rad/s
requires explicit authentication; intent at or above 20 rad/s is rejected.

The current restriction stack is:

| Layer | Joint-speed statement | Other motion restriction |
| --- | --- | --- |
| Integrated ordinary `TRANSIT_SPEED` | requested intent: authenticate above 10 rad/s, reject at/above 20 rad/s | up to 1.2 m free-space request; IK, operational joint range, scene, and exact authorization remain mandatory |
| Basic physical POS_SPEED/POS_VEL path | 5.0 rad/s J1-J3, 10.0 rad/s J4-J6 | requested motor-envelope configuration; also bounded by each motor's configured VMAX |
| Current official Seeed SDK configuration | 5.0 rad/s J1-J3, 3.0 rad/s J4-J6 | Developer configuration, not an autonomous safety qualification |

Endpoint-delta validators now cover each joint's calibrated operational span;
they are corruption guards, not small-motion gates. Duration is representable
from 0.05 to 60 seconds. Integrated publishes both requested and effective
per-joint peak speeds so outside agents can understand authentication and
hardware clamping without relying on a Cartesian-speed proxy.

Seeed's April 2026 B601-DM V4-motor test recommends load below 1.5 kg, radius
below 70% reach (450 mm), and speed below 70% of maximum. Motor 2 thermal
protection ended the extreme trials. Incorporate motor temperature and
load/reach derating into `HARDWARE_QUALIFIED`; do not treat the raw SDK VMAX as
a continuously usable whole-arm speed.

- [ ] Add versioned `BRINGUP_RESTRICTED`, `PERCEPTION_QUALIFIED`, and
  `HARDWARE_QUALIFIED` operating profiles.
- [ ] Preserve calibrated mechanical joint limits, singularity checks,
  payload/effort limits, stopping behavior, and independent emergency stop in
  every profile; "free motion" means use of the qualified arm workspace, not
  removal of hardware protection.
- [x] Remove the arbitrary 20 cm request split for ordinary free-space motion
  and accept a complete goal inside the 1.2 m arm ROI. Keep contact motion
  independently bounded.
- [ ] Derive joint velocity, acceleration, jerk, Cartesian speed, and stopping
  distance from the installed arm identity, official configuration, measured
  payload, transport performance, and staged physical tests. Publish the
  effective values and their evidence revision through capability discovery.
- [ ] Resolve the source conflict between the Seeed SDK's Kd 8 for J1-J3 and
  the Damiao manual's documented MIT Kd range of `[0, 5]` against the installed
  firmware/protocol before promoting high-Kp float or lock profiles.
- [ ] Qualify the current Basic-provider caps with measured arrival, overshoot,
  oscillation, temperature, serial-loss, emergency-stop, and obstacle-stop
  tests before treating them as an autonomous continuous-duty envelope.
- [ ] Revisit conservative operational joint margins against the official URDF
  and measured physical stops. Expand only where calibration proves usable
  range and cable/structure clearance.
- [ ] Keep no-contact and human/unknown-obstacle slowdown policies independent
  of the general free-space motion profile.

The 2026-08-04 physical regression completed a 25 cm world-up POS_SPEED move,
which proves the retired 20 cm split no longer blocks ordinary execution. It
does not by itself qualify collision stopping, temperature, payload, or the
full installed speed envelope.

### Post-motion arm-root refinement TODO

The no-contact adapter now captures before/after RGB-D effector points and
their measured controller-frame positions after successful moves. It retains
up to 48 epoch-keyed correspondences, fits an averaged rigid transform with
Kabsch after at least three non-collinear controlled positions, reports
residual and correction bounds, and recommends an approximately orthogonal
25 cm next calibration direction when the geometry is still insufficient.

One before/after point movement supplies two point correspondences, but it
does not determine rotation about that movement line. A full six-DoF root
correction therefore requires either at least three non-collinear point
positions accumulated across multiple movements or a reliable full-pose
visual effector observation. Six scalar point coordinates alone are not six
independent rigid-transform constraints.

The 2026-08-05 functional test is accepted as a no-major-flaw checkpoint. A
0.25 m world +Z motion produced controller-confirmed arrival and visually
confirmed displacement `[-0.0149, 0.0007, 0.2584] m`, with direction cosine
0.9983 and 14.9 mm lateral error. The reported
`MORE_NONCOLLINEAR_MOTION_REQUIRED` result was correct for two point
correspondences; it did not activate a transform.

The implementation-ready sequence, Fabric ownership, close-range refinement,
and validation criteria are specified in
[Gripper-Motion Arm-Root Alignment](13_GRIPPER_MOTION_ARM_ROOT_ALIGNMENT.md).

- [ ] Publish the refinement samples and candidate through a Fabric-owned,
  versioned contract instead of keeping the accumulator inside one adapter
  process.
- [ ] Add a controller-owned calibration routine that previews and executes
  two or more large, non-collinear, table-cleared movements and reobserves the
  effector after each arrival.
- [ ] Add reviewed candidate activation, rollback, epoch supersession, and
  residual/conditioning thresholds before a refinement can replace the active
  stationary world-to-arm transform.
- [ ] When reliable full-pose gripper tracking is available, fuse its
  orientation constraints with point correspondences rather than discarding
  them.
- [ ] Preserve general point-cloud geometry at distance and add adaptive dense,
  overlapping spheres only for the selected workpiece at very close range.
- [x] Prevent generic alignment language from automatically starting
  FoundationPose. The regular Agent route now requires the exact documented
  FoundationPose request; movement-based generic alignment remains the next
  implementation milestone.

### Controller-owned multistep routing TODO

- [ ] Let one Agent/Skill request describe the final goal and task policy while
  Integrated searches and executes a collision-free multileg route.
- [ ] Include clearance, retreat, lateral, observation, and final-approach legs
  as controller-owned plan elements rather than additional Agent dialogue.
- [ ] Bind the complete route and bounded local-replan envelope to one scene,
  policy, preview, and authorization lineage; require a new decision if the
  goal or contact policy changes.
- [ ] Report per-leg arrival, clearance, scene changes, replans, and final
  hold/release state in the structured execution result.

This routing work has a scene-compiler and live-execution-monitor dependency.
Its scheduling priority remains to be assigned, but it should be completed
before the perception-qualified profile raises the current speed envelope.

### Consolidated spatial-autonomy priority plan

The immediate milestone is an uncertainty-aware, no-contact approach to one
selected object. The task succeeds only when fresh post-motion item and
effector observations prove that the controlled frame is inside the declared
standoff tolerance. A controller arrival result alone is not success.

#### P0: restore and connect trustworthy metric perception

- [ ] Fix the current Femto Bolt Windows Media Foundation access failure
  (`0x80070005`) and add an acceptance test that acquires fresh RGB, registered
  and native depth, intrinsics, and timestamps through the same live binding.
- [x] Produce current arm-link/gripper exclusion geometry from the measured
  robot model and transform revision. The semantic-scene builder must continue
  refusing raw point clouds when that self-filter is absent or stale. The HOT
  Provider now samples the current Fabric arm-link transform chain; physical
  geometry/radius qualification remains.
- [x] Add one HOT `world_model.arm_scene_compiler` Resource Provider as the owner of
  `physical_agent.arm_semantic_sphere_scene`: merge source TTLs, exclude the
  robot and selected semantic objects from raw points, enforce the 0.5 m/20 mm
  gripper layer and 1.2 m/60 mm base layer, then publish one monotonic Fabric
  revision. Fabric now regulates external point-cloud and semantic-assertion
  inputs, metric `locate_item` refreshes a short-lived workpiece assertion, and
  `inspect_arm_semantic_scene` is the read-only test surface. Physical
  camera/transform/self-filter qualification remains.
- [ ] Feed that exact fresh scene into Integrated and prove that expiry,
  source loss, self-filter revision change, and newly occupied clearance stop
  or prevent authorized transit.

#### P1: complete the no-contact closed loop

- [x] Upgrade the existing observe-item path instead of adding another weak
  RGB-only locator; expose metric, task-plane, and bearing-only results.
- [x] Acquire item and `locate-effector-front` evidence concurrently and build
  a common-arm-base uncertainty-aware standoff correction.
- [x] Project current controller FK into the RGB-D frame and reject an
  arm-base visual effector point when FK is unavailable, the point is outside
  the 1.2 m arm ROI, or visual/FK separation exceeds 0.4 m.
- [x] Preserve current controller FK as an explicitly degraded effector
  reference when the VLM-selected gripper surface has no valid exact depth.
  Carry 40 mm uncertainty into standoff planning and never report this path as
  a visual metric localization.
- [x] Bind a ready correction to the exact current camera identity, VIO epoch,
  mounted-workcell activation, and canonical scene revision, then require an
  Integrated controller-owned shadow preview to pass the complete preview
  contract. This step submits no motion and grants no physical authority.
- [x] Turn the current read-only correction plan into a finite orchestrator:
  bind exact authorization to the accepted preview, execute one bounded leg,
  select a short leased settling profile, and always reobserve both item and
  effector before the next leg.
- [ ] Stop on target ambiguity, calibration/frame mismatch, stale evidence,
  increasing residual, unavailable obstacle clearance, iteration limit, or
  the no-contact boundary. Do not convert a toilet-paper `WORKPIECE` assertion
  into contact permission when the task explicitly says no contact.
- [ ] Use the post-movement effector residual to update an explicit bounded
  base-to-arm correction estimate for the next plan. Keep the estimator keyed
  by transform epoch so the same composition remains usable when a future base
  locomotion mechanism changes the base pose.

#### P2: qualify retention and release a useful motion profile

- [x] Implement leased gravity float, 2x-4x Kp compliant hold, and low-speed
  POS_VEL position lock with automatic expiry to float.
- [ ] Characterize compliant hold and position lock under small external
  disturbances, representative payload, thermal soak, lease loss, serial
  fault, Manager loss, and emergency release. Resolve the Kd 8 versus Kd 5
  protocol/source conflict before any higher-stiffness default.
- [ ] Add the versioned `PERCEPTION_QUALIFIED` profile only after P0 execution
  monitoring passes. Raise first toward the Basic provider's measured caps,
  not the SDK maxima, and publish the effective velocity, acceleration, jerk,
  workspace, payload, reach, temperature, and stopping evidence revision.

#### P3: make depth failure an active-perception state

- [ ] Hardware-test the locator fallback ladder on opaque, thin, reflective,
  and transparent targets. Never use background depth for a failed target
  surface and never promote bearing-only evidence to collision geometry.
- [ ] Add bounded multi-view/parallax and image-servo observation actions that
  can improve bearing-only evidence without touching the target. Each action
  must pass the same scene-aware preview and authorization path as approach
  motion.
- [ ] Add known-size/support-plane constraints only with explicit evidence and
  uncertainty; preserve the generic item contract for outside Skills/Agents.

#### P4: reduce Agent turns with controller-owned routing

- [ ] Replace the fixed route alternatives with scene-aware multistep search,
  immutable complete-route preview, controller-owned per-leg arrival, and a
  bounded local-replan envelope. One high-level Agent command should remain
  sufficient while goal, selected object, contact policy, and authority remain
  unchanged.

#### Components to keep out of the physical loop or clean up later

- The generic RGB `analyze_visual_scene` path is useful for language and
  identity but is not a metric locator or collision source. Do not let tool
  discovery choose it as a substitute for `locate_item`.
- `WorldPointCloudAccumulator` is not yet a canonical arm-scene producer. Its
  unfiltered world-frame output must not be connected directly to Integrated.
- FoundationPose remains a known-CAD, reviewed-mask path; it is not a generic
  reflective/transparent item-depth fallback, and expired BufferRefs currently
  remove it from the tested loop.
- Direct relative-move and legacy staged-scene paths remain useful diagnostics,
  but the autonomous approach should use the exact authorized transit and
  canonical scene lineage. Once the P1 loop is stable, remove duplicate Agent
  dialogue/orchestration that performs the same intermediate steps.

### 2026-08-04 explicit-scene and no-contact live qualification

The first end-to-end live qualification of the revised path passed:

- One HOT request for `perception.sam2_scene_tracker` caused Manager to start
  and transition its declared camera and Basic-arm dependencies before SAM2.
  Skills continued to consume Fabric evidence rather than directly activating
  or polling those upstream Providers.
- The user-described table/support-only policy passed VLM mask review and
  compiled 268 `KEEP_OUT` spheres for only
  `table_and_support_surface`. The Agent result registered switchable RGB,
  registered-depth, and reviewed-mask visual evidence, while the 3D viewer
  retained its reduced scene-sphere display.
- The no-contact loop localized the toilet-paper roll and gripper, obtained an
  Integrated scene-aware preview, and completed two measured-arrival
  horizontal corrections of 191 mm and 27.8 mm while preserving height and
  orientation. Post-motion re-observation returned
  `ALIGNED_AT_NO_CONTACT_STANDOFF`: estimated separation 113 mm for a requested
  100 mm standoff, accepted under 23 mm combined uncertainty and an 8 mm
  alignment tolerance. The latest accepted preview reported about 18 mm
  minimum modeled table clearance.
- Safe-home then completed with no position or velocity failure joints,
  preserved the gripper angle, and returned to gravity float. The bounded
  workspace shutdown left no listeners on Manager, Fabric, Agent, controller,
  or perception ports.

This closes the earlier composition/motion-submission blocker for the tested
opaque toilet-paper case. It does not yet qualify general collision-stop
behavior, multi-object ambiguity, reflective/transparent depth fallback,
long-run base-transform adjustment, high-speed profiles, or controller-owned
multistep routing.

### 2026-08-03 toilet-paper run evidence

The latest autonomous journal confirms that the missing link is composition,
not basic visual recognition:

- The stationary world/base calibration completed and was activated with a
  reported bounded residual of about 2.7 mm translation and 0.058 rad
  rotation. The transform path was usable for world-relative motion.
- A requested 20 cm world-up move produced a valid 6-DOF IK preview and sent
  173 frames, but returned `DEADLINE_FLOAT_BEFORE_ARRIVAL`. Its deadline
  Cartesian position residual was about 5.47 mm, so the controller floated
  before confirming the target. Post-motion effector measurement and adaptive
  continuation are still required; a profile-selectable leased hold/lock state
  has now been added and initially hardware-checked.
- The toilet-paper roll was found with high RGB confidence at normalized image
  point `(0.498, 0.740)`. The next request found the gripper opening at
  `(0.507, 0.285)`. Both results were explicitly only 2D; no metric item or
  effector location was produced for the Agent to compare.
- The camera advertised healthy aligned-depth and point-cloud capabilities,
  but Agent discovery selected only the RGB `analyze_visual_scene` tool. The
  known-CAD FoundationPose primitive was not a generic fallback: its Provider
  was exited/unhealthy in the runtime snapshot with an expired BufferRef, and
  its finite Skill remains non-discoverable because it requires a known model
  and reviewed mask.

The existing identify/observe path is now a discoverable typed item-locator
adapter with metric, task-plane, and bearing-only outcomes, and a reusable
nonphysical planner now composes it concurrently with
`locate-effector-front`. A ready correction is now bound to the exact current
camera, VIO, mounted-workcell, and semantic-scene identities and sent to
Integrated for a nonphysical controller-owned preview. Remaining work is to
bind exact authorization, execute one bounded leg, reobserve both after every
move, update the base-frame residual, and repeat until inside a declared
uncertainty-aware tolerance.

### 2026-08-03 routing and local RGB-D retest

A later three-prompt test exposed two intent-routing failures rather than
camera geometry failures:

- `establish the world axis (not the arm base)` incorrectly selected the
  high-latency stationary world-to-arm calibration. The regular Agent now
  deterministically excludes that tool for explicit world-only wording and
  inspects the Local VIO epoch instead.
- `identify ... and 3d locate it` selected the RGB-only scene analyzer because
  the active `PHASE4_ELIGIBLE_TOOLS` file had never been migrated to include
  `locate_item`, even though the global catalog displayed the Skill. Setup now
  migrates existing allowlists and all templates include the composed spatial
  tools.
- `move the gripper ... close to the toilet paper roll` now selects the
  no-contact composition planner and must not fall back to asking for a manual
  relative XYZ displacement.

After the fix, Local VIO established a convention-V2 world frame and remained
`TRACKING` without any arm-base locator. A local, read-only registration of the
previous run's normalized toilet-paper seed `(498, 808)` against a fresh
1920x1080 RGB-D bundle selected pixel `(956, 873)`, measured 0.755 m camera
depth from 49/49 valid samples with about 1 mm median absolute deviation, and
returned VIO-world candidate point approximately
`(0.5965, -0.0779, -0.7944) m`. This proves current depth and timestamped
camera-to-world geometry, but the cross-frame semantic association is not
fresh enough to authorize motion.

The remaining live semantic test requires current camera evidence to be sent
to one of the configured external Gemini/OpenAI VLM providers. The sandbox
correctly blocked that external transfer during this review. Do not bypass the
boundary: obtain explicit operator approval for current-image transmission or
use a future local semantic backend before promoting the candidate to current
item identity or physical approach evidence.

### 2026-08-03 controller-consistency and world-readiness retest

The exact world-only prompt completed against a fresh bounded workspace with
Local VIO in `TRACKING` and explicitly reported that it neither reset the
world origin nor located the arm base. This confirms that the stationary IMU
initialization gate can recover world readiness without paying the stationary
workcell-calibration latency.

The preceding toilet-paper approach attempt exposed an unsafe semantic
association before any physical motion: the item registered near
`[0.417, -0.116, 0.085] m` in arm-base coordinates, controller FK placed the
tool near `[0.258, 0.000, 0.213] m`, but the visual effector locator selected
background depth and returned approximately
`[-2.237, -0.493, -0.669] m`. The resulting 2.78 m apparent item distance was
not a usable plan. The locator now projects FK into its VLM evidence and hard
rejects this class of result before the no-contact planner can emit a target.

The composed approach remains planning-only. It can recover a missing
world-to-arm registration and now returns structured perception rejection,
and its next iteration binds the correction to an Integrated nonphysical
preview. It does not yet bind authorization, execution, and mandatory
post-move re-observation. Therefore “move close” is not yet an autonomous
physical-motion capability, and a nonphysical plan must not be reported as
successful movement.

### 2026-08-03 toilet-paper item-box retest

The four latest journaled `3d locate the toilet paper roll` prompts each made
exactly one `locate_item` call and produced one visual-evidence record. The UI
appeared to show three results because the item locator reused an effector
diagnostic composite containing three panels and deliberately copied the same
annotation onto every panel.

The apparent narrow Gemini boxes were not primarily model-quality failures.
Gemini Robotics returned its usual normalized 0-1000 image coordinates, while
the item host interpreted those integers as native registered-depth pixels.
For the latest 1920x1080 image, the logged point `[678, 589]` was drawn at
native pixel `[678, 589]`; the correct normalized conversion is approximately
`[732, 1130]`, centered on the visible roll. Likewise, logged box
`[588, 546, 772, 633]` converts to native box
`[635, 1048, 834, 1216]`, which encloses the roll. The same incompatible
contract also affected effector-front localization.

Both paths now request an explicit `NORMALIZED_0_1000` coordinate space and
use one shared deterministic conversion into half-open registered-depth pixel
bounds. Version 1 native-pixel payloads remain accepted for outside adapters.
The effector VLM no longer receives a three-panel canvas; it receives one
co-registered RGB view with invalid depth dimmed. The UI separately exposes
switchable RGB, registered-depth, and RGB-depth overlay channels, so changing
diagnostic presentation cannot change geometry. An explicit optical-axis
regression confirms camera X-right/Y-down/Z-forward deprojection maps through
a level camera transform to world X-forward/Y-left/Z-up; no downstream X/Y
swap was added.

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

## Later operator-prompt streamlining

- [ ] Add a non-blocking camera-VLM check when integrated motion returns
  `ARM_MOUNT_CONFIRMATION_REQUIRED` because the world and arm axes have not
  been established. Ask the visual Skill whether the visible arm base is
  upright on a horizontal mounting plane, with base +Z opposite gravity and
  base +X toward the robot/workcell front. Continue automatically only when
  the Skill returns an explicit, sufficiently confident confirmation with
  current-image evidence; if the view is missing, ambiguous, or cannot support
  both axis claims, fall back to the existing operator `y/n` question. Record
  the image reference, model result, confidence, and fallback reason. Treat
  this as a bounded installation attestation, not as geometric calibration or
  safety-rated perception.

## Safety roadmap

Before expanding autonomous robot motion:

- broaden fault-injection qualification for fenced Control Authority Leases;
- qualify safe relinquish on expiry, process failure, Manager disconnect, and lost upstream authority;
- complete physical acceptance for every advertised motion profile;
- keep emergency stop independent from software recovery;
- review every hardware-specific Provider separately.
- physically qualify explicit frame and resolved-vector enforcement for
  semantic Cartesian commands across representative mounts.
