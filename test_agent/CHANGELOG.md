# Changelog

## 0.4.9 - 2026-08-05

- Require the operator's documented exact FoundationPose request before the
  regular Agent loads the long-running stationary calibration runtime. Generic
  alignment language now returns a structured explicit-invocation result and
  never silently falls back to FoundationPose.
- Add the durable next-iteration gripper-motion arm-root alignment plan and
  handoff, including the accepted two-correspondence physical checkpoint.

## 0.4.8 - 2026-08-04

- Expand ordinary free-space relative motion to the full 1.2 m request
  envelope and make POS_SPEED the required Integrated capability. Remove the
  independent Cartesian-speed schema ceiling: requested speed now derives
  per-joint authentication above 10 rad/s and hard rejection at 20 rad/s.
- Capture measured controller FK and RGB-D effector points before and after
  successful moves. Accumulate non-collinear correspondences into an averaged
  arm-root refinement candidate and recommend the next orthogonal 25 cm
  controller-previewed calibration move when more geometry is required.
- Replace the model-copied no-contact execution payload with one opaque
  pending `plan_id`. The host now recovers exact distance, speed, intent,
  orientation policy, request digest, and preview digest from its accepted
  controller preview. This removes the stale 20 cm execution-tool schema cap
  that rejected valid 22.5 cm and larger full-destination plans before commit,
  while host authorization still evaluates the canonical stored envelope.

## 0.4.7 - 2026-08-04

- Plan ordinary no-contact requests to the complete uncertainty-expanded
  destination instead of an arbitrary 5 cm segment, at a 0.12 m/s nominal
  endpoint speed. Post-move paired observation still owns residual alignment.
- Automatically retry transient item/effector observation rejection twice and
  rebuild one stale, expired, start-drifted, or collision-changed controller
  preview from fresh evidence inside the authorized execution workflow.
- Convert a stationary-calibration candidate digest mismatch into an exact
  recoverable continuation, and recover malformed activation arguments from
  the current persisted continuation without weakening digest verification.
- Give every explicit obstacle remap a new mapping epoch and reject a compiled
  scene unless it names that exact policy revision.
- Open switchable RGB, registered-depth, and reviewed SAM2-mask visual evidence
  after a successful user-requested map, with the complete and per-object
  sphere counts retained in the inspection result.
- Raise the agentic operation deadline from 90 to 300 seconds while retaining
  the independent 30-second idle-progress and per-VLM-attempt limits. A valid
  move/reobserve/replan loop is no longer cancelled midway through its second
  paired observation.
- Rely on Manager-owned Provider dependency activation for scene tracking and
  compilation, so Skills continue to read best-available Fabric data without
  directly jamming camera or arm Providers.

## 0.4.6 - 2026-08-04

- Keep a direct-motion approval pending until the Integrated Controller has
  actually accepted its one-shot trigger. If a periodic semantic-scene update
  invalidates only the controller-owned preview, re-preview the exact approved
  target once against the newest scene instead of failing or consuming the
  approval; spatial-frame and measured-start checks still run before commit.
- Treat a missing VIO session epoch as advisory provenance for the reviewed
  `MOUNTED_CANONICAL_CAMERA_CALIBRATION_GATED_V2` workcell policy. Stationary
  camera-to-arm motion is bound to canonical camera calibration and the active
  reviewed transform, while the older V1 identity policy remains epoch-bound.
- Validate Integrated relative-delta transit previews against their actual
  `position_delta_m` request instead of reconstructing an unrelated absolute
  `position_m` contract and rejecting the controller's valid response.
- Keep an exact-depth visual effector reference eligible for bounded control
  math when controller FK is temporarily absent. Capture-time FK still has
  priority, Fabric-latest FK is the second choice, and the 40 mm-uncertainty
  visual fallback sends only a capped relative correction that Integrated
  resolves from its own fresh measured controlled frame.
- Persist the last Fabric-accepted, explicitly user-authored SAM2 scene policy
  in runtime state and restore it through Fabric after workspace restart. A
  missing state file still means no policy; no table or black-mat default is
  introduced.
- Route direct `move ... above/toward <item>` wording, including commands that
  omit the effector noun, into the reviewed no-contact item-approach workflow
  instead of attempting to invent a work-object-only scene policy.
- Render semantic scene spheres by unique `sphere_id` instead of collapsing
  every voxel for one object onto its shared `object_id`.
- Show a deterministic maximum of 240 compiled scene spheres while reporting
  both displayed and complete controller-scene counts in the 3D viewer.
- Preserve mounted arm-base item markers by representing an intentionally
  absent VIO epoch as JSON null rather than the string `"None"`.
- Evaluate Fabric RGB-D bundle freshness at the exact shared-memory copy time
  rather than rejecting it after unrelated binding, VLM, scene, or Agent
  latency; later task-age and controller commit fences remain independent.
- Require explicit obstacle scans to activate both SAM2 and the arm scene
  compiler and verify a fresh nonempty KEEP_OUT scene before claiming success.
- Treat Integrated `WAITING_NEXT`, `HOLDING_FINAL`, and verified
  `COMPLETED_FLOAT` as measured arrivals and continue mandatory re-observation.

## 0.4.5 - 2026-08-03

- Add independent 3D-viewer toggles and a color legend for keep-out,
  pushable, work-object, and live gripper markers.
- Evaluate VIO freshness at the exact captured RGB-D timestamp so downstream
  scene compilation latency cannot invalidate evidence retroactively.
- Return a structured HOT scene-compiler activation and exact retry when a
  no-contact plan has metric landmarks but no current semantic-scene revision.
- Carry explicit FREE_3D, NO_DESCENT, and same-height vertical policies through
  prerequisite recovery and every re-observation iteration.
- Request per-plan POSE_6DOF IK so no-contact transit preserves the current
  controlled-frame orientation without changing global controller settings.
- Draw metric-item and semantic workpiece markers from their estimated volume
  centroid rather than the measured front-surface control point.
- Fall back to `netstat` when non-elevated PowerShell cannot resolve a healthy
  loopback listener with `Get-NetTCPConnection` during bounded startup.

## 0.4.4 - 2026-08-03

- Correct the low-level VLM geometry contract used by item and effector
  localization. Gemini Robotics' normalized 0-1000 coordinates are now mapped
  once to registered-depth pixels instead of being mistaken for native
  1920x1080 pixels; source and converted coordinates remain auditable.
- Expose RGB, registered depth, and RGB-depth overlay as switchable visual
  evidence channels for item and effector localization.
- Preserve the last trusted metric item marker across rejected retries within
  the same VIO epoch, marking it stale after 60 seconds instead of silently
  removing the diagnostic sphere.
- Give `locate_item` one clean RGB view on the registered-depth grid instead
  of the three-panel effector diagnostic composite. The visual result now has
  one item box and one selected pixel rather than triplicated annotations.
- Reject a purported whole-item box when its robust local color separation is
  indistinguishable from its surrounding support/background. Rejected boxes
  retain their audit evidence but cannot publish a workpiece assertion or
  enter controller math.
- Bind each ready no-contact correction to the current semantic-scene
  revision, exact camera binding/boot/calibration, VIO epoch, and permanent
  mounted-workcell activation, then request and validate an Integrated
  controller shadow preview. Planning remains nonphysical and grants no
  execution authority.
- Add a separate Agent-SDK-authorized `execute_no_contact_approach_step` tool.
  It accepts only the current plan's exact preview IDs and digests, mints a
  decision-bound controller assertion after host authorization, executes at
  most one capped correction, and returns directly to parallel item/effector
  re-observation and replanning.
- Register successful metric `locate_item` results as annotated visual
  evidence, including the selected item box/pixel and any metric neighbor
  samples, so the Agent UI opens the exact image used by the Skill.
- Add `/api/world-annotations` and semantic wireframe markers to the developer
  3D world viewer for the latest located item plus current `WORKPIECE`,
  `PUSHABLE`, and `KEEP_OUT` scene spheres.
- Replace the five-minute mounted world-to-arm activation expiry with
  `MOUNTED_IDENTITY_TRACKING_GATED_V1`: transient tracking loss suspends motion
  usability and recovery restores it, while camera/calibration/VIO identity
  changes invalidate the activation.
- When visual effector depth is unavailable, retain the current controller FK
  endpoint as an explicitly degraded 40 mm-uncertainty control reference. The
  result never claims visual metric localization and the no-contact planner
  expands its standoff by that uncertainty.
- Added the discoverable read-only `inspect_arm_semantic_scene` Skill adapter.
- Publish successful metric `locate_item` results as short-lived canonical
  Fabric `WORKPIECE` assertions while preserving valid location evidence if
  semantic publication fails.
- Wired the HOT arm scene compiler inspection tool into the regular Agent
  allowlist and setup migration.

## 0.4.3 - 2026-08-03

- Route explicit world-axis-only requests through bounded Local VIO readiness:
  start camera/VIO as needed, acquire the stationary initialization inhibit,
  verify a current `TRACKING` body pose, and exclude world reset and the slow
  stationary world-to-arm-base calibration path.
- Route explicit 3D/metric item-location requests only to `locate_item`, and
  route move-close wording to the composed no-contact planner instead of
  asking the caller to supply relative XYZ motion.
- Added the `CURRENT_WORLD` target alias, resolved from the synchronized RGB-D
  frame's exact VIO world identity, so item and point registration do not need
  an arm-base pose.
- Added typed loopback endpoints for deterministic item location and supplied
  RGB-pixel registration. These expose the existing Skill boundary to outside
  agents without depending on conversational tool selection.
- Migrated old `PHASE4_ELIGIBLE_TOOLS` configurations to include item,
  effector, and no-contact approach tools; updated every configuration
  template so discovery and execution eligibility cannot silently diverge.
- Normalized duplicate Windows `Path`/`PATH` process keys and hid the Agent
  background window in the launcher.
- Record and stop the actual system-Python Agent listener behind the Windows
  venv launcher, verify its `/health` service identity before orphan cleanup,
  and fail shutdown if a verified Agent still owns TCP port 8000.
- Add controller-FK association to arm-base effector localization. The VLM
  receives a projected tool-pixel/depth prior, while post-registration policy
  rejects a visual point outside the 1.2 m arm ROI, more than 0.4 m from FK,
  or lacking a current controller reference.
- Return missing arm-base registration and rejected item/effector observations
  as structured non-motion continuation states instead of raw tool errors.

## 0.4.2 - 2026-07-31

- Offer or activate a stationary calibration only when it carries the current
  single mesh-centered orientation proof. Older candidates return
  `CANDIDATE_ORIENTATION_SUPERSEDED` and require a fresh calibration rather
  than replaying a transform whose semantic root may be on the wrong side of
  an inverted FoundationPose hypothesis.
- Derive base +Z directly from the candidate's serialized `world_from_base`
  quaternion and require it to match the reviewed up dot product before an
  activation continuation is offered.

## 0.4.1 - 2026-07-31

- Scope regular and developer model sessions to one Test Agent process boot.
  SQLite history remains stored for audit, but a restarted service no longer
  replays an incomplete `required_next_tool` from an earlier boot. Expired or
  pre-provenance calibration candidates are withheld from continuation and
  return `FRESH_CALIBRATION_REQUIRED` without reaching Manager.
- Preserve Manager's exact reviewed-calibration activation error detail so a
  503 identifies a missing signing secret, camera calibration, or VIO status
  instead of reporting only a generic HTTP failure.
- Classify collision-free, low-residual IK solutions rejected only by
  endpoint or aggregate one-shot travel guards as
  `REACHABLE_BUT_ONE_SHOT_POLICY_LIMITED`. The result remains non-executable,
  reports exact guard excess, and never auto-segments physical motion.
- Added bounded controlled-frame head yaw to Integrated relative-pose motion.
  Pure rotation uses an explicit zero-translation intent, while combined
  translation and yaw are previewed together with `POSE_6DOF` on the existing
  PRESS_MIT one-shot path. Signed yaw is composed as a matrix, exact preview
  arguments and measured orientation are revalidated before execution, and
  the finite Skill hard-limits yaw to 45 degrees.
- Added the same four browser-session authorization controls to the Developer
  Agent and preserved their policy through approval resume. Fresh regular and
  developer pages now default to all four controls enabled, 35 cm, and a
  0.5 m/s authorization ceiling; controller and Skill limits remain separate.
- Preserved the Agents SDK `RunState` context across approval resumes so an
  approved Provider transition executes once and the original Agent task
  continues. The duplicate-operation fingerprint remains a fallback guard
  rather than terminating every correctly approved resume.
- Made an active reviewed world-to-arm transform authoritative for ordinary
  world-language directions as well as explicit signed axes. Upright-mount
  confirmation is now consulted only when measured resolution is unavailable.
- Added optional nominal relative-motion speed. The adapter derives requested
  duration as distance divided by speed, binds requested and Provider-planned
  timing through preview approval and execution, and reports any safety-driven
  duration extension without claiming constant Cartesian velocity.
- Added an independent nominal-speed ceiling to bounded browser-session motion
  authorization and displayed requested speed and planned duration in the
  physical-motion approval.
- Prevented regular and developer Agent approval loops by fingerprinting exact
  protected operations across SDK resume steps and terminating a run if the
  model repeats an already approved or rejected request.
- Returned insufficient stationary-calibration yaw leverage as a structured,
  actionable non-motion result instead of a raw tool exception.
- Replaced the standalone Spatial Axis Inspector rotation widget with the
  developer point-cloud renderer. The shared view now overlays live world,
  arm-base, gripper/tool, camera, six arm-link, object, body, and sensor local
  XYZ frames with per-frame visibility, labels, folded transform metadata,
  fit-to-axes, orbit, pan, zoom, and an opt-in explicit 2D screen-axis overlay.
- Anchor developer RGB-D points to the exact reviewed stationary-camera
  reference while its Manager activation is current for the same VIO epoch.
  The renderer falls back to timestamped live VIO camera poses otherwise,
  clears retained points when transform authority changes, and displays the
  active authority and calibration revision.
- Removed the camera-capability binding status card from the developer prompt
  chrome while retaining binding evidence in the status API, and removed the
  pre-filled screenshot prompt so new developer prompts begin empty.
- Started clean v3 regular/developer Agent sessions while retaining the legacy
  SQLite histories. Model replay targets the most recent 32 session items and
  expands to the enclosing user-turn boundary so required Responses reasoning
  and function-call items cannot be separated.
- Preserved complete current Manager evidence in Agent runtime inspection,
  including Provider reports, controller telemetry, command/target/planning
  state, launch configuration, identities, capabilities, timestamps, and
  non-secret environment values. Only duplicate Skill schemas are omitted;
  credential-like environment values are retained by name and redacted.
- Added a calibration-only 600-second outer deadline and FunctionTool timeout
  for the FoundationPose-backed stationary workcell workflow. Ordinary Agent
  runs retain the 90-second deadline.
- Added browser-session authorization switches for Provider start/HOT/WARM,
  exact Integrated relative-motion previews up to an operator-entered
  centimeter limit, and stationary world-to-arm calibration. Provider stop,
  safe-home, and other protected operations remain separately approval-gated.
- Moved eligible session authorization into the Agents SDK dynamic tool
  approval predicates so authorized calls execute without an approval
  interruption or separate browser resume request. Increased the UI
  authorization-entry ceiling to 100 cm while preserving the motion tool's
  independent 20 cm limit.
- Required explicit signed world-axis motion to use a reviewed motion-usable
  world-to-arm transform instead of falling back to an upright arm
  attestation, and added the labeled world XYZ triad and active frame name to
  the VIO point-cloud view while retaining gravity/down.
- Prevented explicit signed world-axis requests from being intercepted by the
  ordinary-language upright-mount confirmation, including the runtime policy
  branch used by the regular Agent.
- Added an exact candidate review-and-activation continuation for stationary
  calibration. A separate browser-session switch can authorize this bounded
  non-motion operation; Manager still revalidates candidate digest, quality,
  provenance, current VIO tracking, and expiry before publishing a
  motion-usable transform under mounted-rig identity and tracking gates,
  without a wall-clock activation expiry.
- Required the Agent to invoke necessary lifecycle and calibration tools
  directly instead of ending a run with a conversational permission request;
  tool interruptions remain the authorization boundaries.
- Return resolved direction, start/target pose, and per-joint travel/limit
  diagnostics when an Integrated IK preview is rejected instead of collapsing
  the controller result into a generic tool error.

## 0.4.0 - 2026-07-30

- Separated the default upright arm-mount direction assumption from VIO:
  confirmed arm +Z up and +X front can resolve motion without VIO.
- Added an explicit fixed-stationary-VIO-rig confirmation followed by a
  non-resetting tracking check. The check now uses a bounded VIO-local
  stationary attestation instead of global motion inhibit, so it cannot revoke
  Integrated's Basic lease.
- Added an explicit `HOT` recovery route when Integrated reports
  `RECOVERY_REQUIRED`, even if its provider process is already running.
- Added gravity-aligned effector localization before and after relative
  motions, with an independent visual displacement verdict.
- Made fixed-rig before/after evidence best-effort for directions already
  established by an upright-mount confirmation. Missing exact effector depth
  is reported separately and no longer prevents an IK preview.
- Added measured-orientation preservation for relative translation using the
  Integrated Controller's existing `POSE_6DOF` mode, with preview-to-execution
  orientation revalidation.
- Exposed stationary world-to-arm-base calibration on the regular Agent
  surface and documented the explicit Integrated HOT recovery required after
  calibration or Basic safe-home preempts its lease.
- Added one canonical semantic-direction resolver using world +X front, +Y
  left, and +Z up, with timestamped conversion into the arm-base frame.
- Added explicit arm-mount and gravity-leveled camera confirmation workflows
  and rejected stale, degraded, missing, or convention-mismatched evidence.
- Added the `/dev/spatial-axes` live frame inspector and migrated the world
  point-cloud view to Z-up.
- Required explicit camera optical and convention-V2 metadata before RGB-D
  registration or point-cloud conversion.

## 0.3.1 - 2026-07-29

- Made the launcher put this workspace's Test Agent, Provider, and Skill Python
  source roots ahead of ambient installations so an adjacent checkout cannot
  silently supply execution code.
- Added provider-local latest-reference RGB-D fallback. It copies a fresh
  synchronized RGB and aligned-depth pair directly from the current shared
  memory mapping when a Fabric-published ring reference has already recycled,
  while preserving the provider's timestamp and synchronization checks.
- Added the discoverable `execute_reviewed_observation_motion` adapter. Its
  OpenAI Agents SDK schema accepts only an approved decision ID and exposes no
  coordinate, speed, mode, contact, lease, safe-home, or fallback-motion
  argument.
- Before assertion issuance, reviewed execution now restages the exact
  authorized semantic scene and verifies that Integrated accepted the same
  scene revision, arm-base frame, and collision-sphere array.
- Completed a real Agents SDK no-contact transit above the toilet roll:
  decision `6fb800ec-5b4b-4242-80ee-f0744c7f6cc8`, plan
  `a47557e6-ea33-4862-a3b9-b6790baee963`, 40/40 stages, 0.25 rad/s joint
  ceiling, 0.05 m/s Cartesian ceiling, 0.07943 m modeled minimum clearance,
  0.001185 m final controlled-frame error, and 0.099942 m measured vertical
  standoff.
- The successful execution ended in
  `HOLDING_AUTHORIZED_TRANSIT_ENDPOINT`; no implicit release, gravity-float, or
  safe-home was issued by the agent.
- A later read-only status check found that a subsequent platform/authority
  loss had changed the retained transit to `RELEASED` and left Integrated
  `DEGRADED`. This post-completion transition is recorded separately and does
  not rewrite the successful execution result.
- Unified the browser development GUI around the shared dark
  white/gray/black palette. Source images and semantic status/warning colors
  remain colored where that color carries information.
- Confirmed final shutdown of the standalone Test Agent after the arm
  safe-home sequence; final endpoint and process checks found no remaining
  Test Agent listener or process.
- Documented that natural-language Cartesian directions remain an external
  semantic/frame-resolution problem and are not accepted as raw model-selected
  axis commands by the decision-ID-only execution adapter.
- Raised the clean publication regression result to 93/93 Test Agent tests.
- Added monorepo source-root setup for the Integrated Controller and finite
  Skill packages used by the current Agent.
- Declared the existing JSON Schema runtime dependency and moved the
  Agents-SDK-only schema-test import behind the optional-SDK skip. This keeps
  the protected legacy GitHub workflow useful without concealing failures when
  the complete Test Agent dependency set is installed.
- Marked the Windows named-shared-memory replay module as platform-specific so
  Linux CI skips those ten tests while Windows publication validation continues
  to execute them.
- Made hosted-model SDK imports backend-local at use time so read-only routing
  and offline tests can load without installing every optional VLM backend.
- Added monorepo test source-root setup and made the disabled cutting-manifest
  assertion conditional because that prototype is intentionally absent from
  the GitHub publication.

## 0.3.0

- Added per-Skill source-time and association policy. Fabric receipt time and
  producer freshness remain evidence and do not replace the source timestamp;
  replay can explicitly accept historical source time without relabeling it.
- Added enforced binding coverage for cold explicit-fallback rejection and
  exact selected/configured-fallback provenance after independent activation.
- Synchronized standalone Test Agent environment templates with the root/core clean baseline and made setup prefer the root recovery copies.
- Added hardware-incapable Phase 5 shared-memory capture/replay with immutable
  BufferRef copies, hash validation, redaction, and deterministic failure
  injection.
- Added finite manifest-bound Agent SDK adapters for stationary calibration,
  general visual analysis, RGB-D spatial registration, and review-only tool
  registration. New consequential Skills remain off the runtime allowlist.
- Added a bounded read-only spatial-registration API with current camera
  binding, provider instance/boot consistency, observation freshness, generic
  route selection, VIO epoch checking, and exact-timestamp Fabric transforms.
- Added review-only tool-frame candidate construction that keeps robot boot and
  VIO transform epochs separate and never publishes a control frame.
- Fixed multi-tool callback schema validation so each Agent tool retains its
  own manifest descriptor instead of the final descriptor in the build loop.
- Added a decision-specific execution-assertion route and a separate
  observation-motion execution route. Assertions are signed, short-lived,
  exact-preview-bound, issued once, and stored only by ID and hash.

## 0.2.9

- Show initialization accel/gyro window counts and inferred sample rates in the Pose propagation card.

## 0.2.8

- Show accelerometer/gyroscope history counts, timestamp skew, and the current VIO initialization blocker.
- Mark an earlier failed startup auto-init as superseded while a later manual initialization is running or has succeeded.

## 0.2.7

- Updated the GUI for inertial-first VIO semantics.
- Renamed the visual state to Visual Correction and the pose state to Pose Propagation.
- Displays whether RGB-D or synchronized IR/depth supplied the accepted correction.
- Displays visual update age, correction magnitude, reprojection error, and acceptance state.
- Displays IMU propagation step count and inertial state timestamp.
- Retained gravity, reset, orthographic isometric point-cloud, camera-frustum, and map-capture diagnostics.

## 0.2.6

- Waits for the VIO Provider to acknowledge motion inhibit before requesting initialization.
- Recovers startup initialization when reset changed epoch but the Manager returned a transient control-response error.
- Added a separate rotation-estimator status light with visual/gyro disagreement and gyro sample diagnostics.
- Distinguishes visual tracking, gyro rotation hold, gravity adjustment, and map-capture states.
