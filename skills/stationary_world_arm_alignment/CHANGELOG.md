# Changelog

## 0.8.9 - 2026-08-05

- Retain FoundationPose as an explicit finite initializer while removing it as
  an automatic Agent-facing base-pose route. The regular Agent must receive the
  documented exact FoundationPose request before loading this runtime.
- Document the movement-based gripper correspondence plan that will replace
  generic long-running base-pose initialization in the next iteration.

## 0.8.8 - 2026-08-04

- Retry one malformed structured VLM response for gripper localization and
  base-axis validation. A second malformed response becomes an explicit
  bounded error instead of leaking a raw JSON decoder exception.

## 0.8.7 - 2026-08-04

- Record the exact RGB-D shared-memory copy time so downstream Skills can
  distinguish freshness at acquisition from later Agent/VLM workflow latency.
- When a finite Fabric BufferRef has already been recycled, retain the bounded
  synchronized mapping recovery while binding temporal evidence to the exact
  copied references and preserving the original Fabric bundle time.

## 0.8.6 - 2026-08-03

- Retain the VLM RGB-D foremost-beak observation from the initial independent
  base fit and derive bounded tool-to-beak geometry for later fast gripper-only
  mounted-base translation refinement.
- Record the camera canonical device identity in new candidates. A later
  refinement may bridge a camera process restart when the physical device,
  calibration revision, provider, and optical frame still match; older
  candidates without device identity retain strict instance/boot matching.

## 0.8.5 - 2026-07-31

- Fix the transform application order for an inverted FoundationPose base
  hypothesis. The exact discrete orientation is now selected at the centered
  CAD mesh origin before `mesh_from_semantic` computes the arm-base/table
  datum; it is no longer painted onto an already displaced semantic root.
- Select at most one proper rotation from identity, X-180, Y-180, or Z-180.
  The choice jointly makes base +Z agree with current VIO/world up and base +X
  face the segmented RGB-D gripper. No sequential 180-degree corrections are
  applied.
- Preserve the observed CAD mesh-center translation exactly. X/Y hemisphere
  corrections intentionally recompute the semantic root across the mesh
  center, while a pure Z-180 front/back correction leaves that root unchanged.
- Record and Manager-verify the raw/corrected up dot products, selected axis,
  correction count, mesh-centered application order, zero correction
  translation, preserved mesh center, and semantic-root adjustment. Older
  candidates without this proof require a fresh calibration.
- Independently derive +Z from the serialized `world_from_base` XYZW
  quaternion during Agent preflight and Manager activation. A candidate cannot
  claim corrected +Z-up while documenting a downward transform for consumers.
- Add nontrivial transform-chain tests covering FoundationPose
  `camera_from_mesh @ mesh_from_semantic`, XYZW serialization, and the
  centered-mesh-to-semantic-root correction equation.
- Load overlay geometry and `mesh_from_semantic` from the same configured live
  model registry used by the FoundationPose execution route, removing the
  former packaged-default/live-registry split authority.

## 0.8.4 - 2026-07-31

- Resolve the base's exact 0/180-degree yaw ambiguity from the VLM-segmented
  gripper's aligned-depth 3D point expressed in the raw base frame. This
  removes perspective and foreshortening errors from the primary +X sign
  decision while preserving the base root translation and +Z direction.
- Retain the bounded RGB overlay direction review only when exact aligned
  gripper depth is unavailable, and record that fallback as a warning.
- Permit each fresh reviewed candidate to replace the prior active workcell
  calibration through Manager supersession rather than waiting for expiry.

## 0.8.3 - 2026-07-31

- Persist the captured VIO Provider ID, instance, boot, and observation time in
  each candidate so Manager can validate the exact current VIO heartbeat
  during activation without re-fetching a transient Fabric status record.
- Simplified post-fit validation to one deterministic projected-box size
  comparison and one categorical visual direction decision. The host compares
  projected and observed base-box width and height, retries when the maximum
  mismatch exceeds 25 percent, and permits at most two FoundationPose
  attempts.
- Limited the pose-review VLM to deciding whether projected base +X faces
  backward, away from the visible gripper. A backward result applies exactly
  180 degrees about the semantic arm-base root's +Z axis; it cannot translate
  the root or choose an intermediate angle. An unclear visual decision is
  retained as a warning without changing the fitted yaw.
- Removed the support-plane, segmented-depth correction, upright-mount,
  controller/gripper lever-arm, point-residual, and VLM confidence/orientation
  gates from base-pose acceptance. FoundationPose translation is no longer
  shifted by those post-fit checks.
- Changed base-up comparison to diagnostic-only behavior. A missing world-up
  direction or a base +Z direction outside the configured warning angle is
  reported but does not reject or modify the calibration.
- When both finite attempts exceed the 25-percent size threshold, the Skill
  returns the closer projected-size result with an explicit warning instead of
  discarding both fits.

## 0.8.2 - 2026-07-31

- Restored the robot base's post-fit yaw ambiguity to an exact 0/180-degree
  choice. The segmented gripper surface is no longer treated as though it were
  the controller TCP for an arbitrary continuous yaw correction.
- Made the validated stationary-camera pose authoritative for the final base
  transform. VIO samples remain available as drift diagnostics but cannot move
  a fixed CAD fit while FoundationPose is running.
- Added a gravity-constrained camera-frame support-plane gate. The CAD semantic
  base origin must be within 10 mm of the table/support datum before a
  calibration candidate can be created or Manager-activated.
- Made Manager activation and Agent-side continuation eligibility require the
  exact 0/180-degree yaw evidence and support-plane evidence, so a pre-fix
  continuous-yaw candidate is not offered for activation or motion use.
- Preserved the mesh-centered upright correction and its semantic translation;
  this keeps the fitted physical mesh invariant while moving the arm-root datum
  to the table side of an upside-down visual hypothesis.

## 0.8.1 - 2026-07-31

- Converted insufficient yaw leverage into an actionable
  `CALIBRATION_POSE_REQUIRED` result with measured controller/visual lever arms,
  and retained segmented-depth and structured failure artifacts for diagnosis.
- Converted yaw-fit NumPy booleans to native JSON booleans, preventing a valid
  calibration from failing during result serialization.
- Fixed the leaked Y-up base correction. Upright workcell calibration now uses
  VIO/world positive Z and resolves an upside-down visual hypothesis around the
  CAD mesh origin, including the semantic-origin translation.
- Replaced the invalid TCP-to-VLM-beak point-distance equation with a
  continuous yaw fit between the segmented RGB-D gripper bearing and the
  controller's base-to-TCP bearing. This handles arbitrary base yaw instead of
  assuming the visual error is limited to 0/180 degrees.
- Matched the FoundationPose mesh near surface to segmented RGB-D base depth
  before yaw fitting, so a stable but depth-shifted pose cannot corrupt the
  base-to-gripper bearing.
- Removed uncalibrated beak and gripper-model origins from base translation
  fusion. Those observations affect translation only when an explicit rigid
  tool-to-feature extrinsic is configured.
- Bounded post-hoc tool-to-beak estimates and excluded implausible estimates
  from later refinement.

## 0.8.0 - 2026-07-30

- Migrated stationary world alignment to positive X forward, positive Y left,
  positive Z up.
- Temporarily removed the former semantic base correction while migrating the
  world convention; version 0.8.1 replaces it with a Z-up, mesh-aware solver.
- Added world and camera-optical convention identifiers to calibration
  candidate schema version 3.
- Rejected every legacy Y-up candidate and activation instead of
  reinterpreting its quaternion.
- Kept robot-base local positive X forward and positive Z model-up unchanged.

## 0.7.1 - 2026-07-29

- Added local reviewed-candidate activation support with stable decision
  identity, signed reviewer evidence, bounded request timeouts, and the
  operator-facing `scripts/review_and_activate.py` helper.
- Ordered calibration startup so the camera and Local VIO can become current
  before Integrated or Basic arm authority is requested. Calibration remains
  stateful but nonphysical and does not command the arm.
- Preserved FoundationPose as a compatibility route owned by this finite
  Skill. It is not advertised as a real-time tracking Provider or a continuous
  source of arm-base truth.
- Added discoverable finite-Skill metadata for stationary workcell calibration
  while keeping activation, provenance, freshness, quality, and motion-inhibit
  gates outside the model prompt.
- Completed and activated calibration
  `20260729T080635Z-7286b758` as
  `world/stationary_camera/20260729T080635Z-7286b758`, bound to VIO epoch
  `aed3c599-6934-4147-86c5-1e98a0cd99f0`. Its reported error bounds are
  0.002728 m and 0.028931 rad.
- Recorded the symmetric-yaw ambiguity from the base estimator. The Skill
  selected the 180-degree alternative using current kinematic gripper
  evidence.
- Recorded an underexposed calibration image limitation. A deterministic gamma
  copy was used only for review; the original RGB-D observation and hashes
  remain authoritative.
- Unified the browser development GUI around the shared dark
  white/gray/black palette while retaining source-image and semantic
  status/warning colors.
- Documented the remaining axis-alignment limitation: the active workcell
  transform relates camera-world and arm-base axes, but natural-language
  directions still require explicit gravity, frame, transform-revision,
  timestamp, and uncertainty semantics.

## 0.7.0

- Added reviewed calibration candidates and Manager-enforced activation.
- Added finite Agent discovery metadata and explicit stationary-scene scope.
- Added current camera, calibration, VIO epoch, transform, motion-inhibit, and
  quality provenance to the motion-usable activation boundary.

Earlier history is described in `README.md` and
`docs/EVALUATION_AND_DESIGN.md`.
