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

- Integrated MIT `ONE_SHOT` and MIT `HOLD_LB` are the only arm motion profiles currently marked usable.
- POS_VEL `ONE_SHOT` is limited to paths at or below 20 cm with no payload or high external load. Greater distance or load is not considered stable.
- POS_VEL `HOLD_LB` is experimental and unstable and is excluded from Manager capability discovery.
- Arm POS_TOR `ONE_SHOT`/CONTACT_WORK is experimental and unstable and is excluded from Manager capability discovery.
- Obstacle-route planning is not implemented. Semantic point-cloud objects can be staged, but the current preview is diagnostic rather than a route-search authority.
- The guarded Manager revision exposes control-authority leases and reviewed authorization decisions. The physical implementation remains prototype-grade and is not safety certified.
- USB/serial transport timeouts and mode-confirmation faults remain physical qualification risks.

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
