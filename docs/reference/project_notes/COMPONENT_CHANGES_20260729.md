# Component Changes from the Agent SDK Physical Test

Date: 2026-07-29

This is the cross-component index for the systematic cleanup and final
OpenAI Agents SDK no-contact physical test. The detailed failure analysis is in
`OPENAI_AGENT_SDK_ROADBLOCKS_20260729.md`. Each component below also has a
local changelog so its behavior can be reviewed without relying on chat
history.

## Platform and agent runtime

- `platform_core`: enforced reviewed workcell activation, signed reviewer and
  execution assertions, transform provenance, motion inhibit, passive Fabric
  data publication, bounded headless startup, local signing-secret bootstrap,
  and stale Manager/Fabric release-binary detection.
- `test_agent`: manifest-based agent discovery, Skill-specific source-time
  policy, generic/direct shared-memory camera routing, spatial and VLM
  adapters, immutable authorization records, exact-preview execution
  assertions, exact semantic-scene refresh, and the decision-ID-only Agents
  SDK execution tool.

## Providers

- `local_vio`: fixed stale two-slot BufferRef consumption by using Fabric for
  metadata discovery and copying current provider-local image references from
  persistent shared memory.
- `orbbec_femto_bolt`: retained its direct shared-memory fallback, published
  flexible channel/alignment metadata for generic consumers, and gained a
  headless external launcher suitable for bounded agent orchestration.
- `rebot_arm_dm`: retained hardware-specific motor safety, gravity-supported
  error fallback, lease fencing, staged mode changes, endpoint support, gripper
  limits, and safe-home behavior; the handover/gripper path was physically
  exercised.
- `rebot_arm_integrated`: became the owner of controller-wide planning,
  singularity/collision/speed policy, exact-preview authorization, direct
  physical submission, synchronous audit, upstream lineage, and authorized
  endpoint hold.
- `foundation_pose`: no publication-oriented redesign was made in this pass.
  It remains a slow compatibility engine requested by the finite stationary
  calibration Skill rather than a real-time tracking authority.

## Skills

- `stationary_world_arm_alignment`: finite stationary workcell calibration,
  base and effector evidence fusion, reviewed candidate generation, and
  Manager-enforced activation. It supersedes treating slow foundation pose as
  a continuously useful alignment Provider.
- `spatial_registration_rgbd`: read-only conversion from a selected RGB point
  to timestamped 3D coordinates across flexible camera grids and alignments.
- `verify_rgbd_alignment`: read-only numeric and VLM inspection of actual RGB-D
  content, synchronization, boundary, and registration quality.
- `visual_scene_analysis`: general finite VLM question routing with provenance
  and bounded fallback.
- `locate-effector-front`: general VLM landmark for the most distal reliable
  depth point, or both fronts and their registered 3D mean for a bare
  two-jaw gripper.
- `register_tool_to_control_frame`: review-only candidate construction for a
  tool acting point/control frame, including reflective-tool nearby-depth
  behavior.
- `identify_pointed_object`: read-only identification of the object indicated
  by a person in a current RGB scene.
- `observe_pointed_object`: future front/top observation workflow contract;
  deliberately nondiscoverable until its structured point and complete
  nonphysical adapter are implemented.
- `execute-reviewed-observation-motion`: narrow physical Skill selected by the
  real Agents SDK. The model receives only a decision ID; the host and
  controller own every motion-bearing value.
- `vegetable_cutting`: preserved locally as a nondiscoverable supervised
  prototype. A separate axial slicing Skill remains future work.

## Completed end-to-end evidence

- The actual OpenAI Agents SDK selected
  `execute_reviewed_observation_motion`.
- The host resolved approved decision
  `6fb800ec-5b4b-4242-80ee-f0744c7f6cc8`.
- Integrated accepted plan `a47557e6-ea33-4862-a3b9-b6790baee963` with
  one-time assertion `6f6cd56c-b6e8-4908-ab93-95c64eb6a0ee`.
- The controller completed 40/40 stages with a 0.25 rad/s joint-speed ceiling,
  0.05 m/s Cartesian ceiling, 0.07943 m modeled minimum clearance, no
  reported fault, 0.001185 m final controlled-frame error, and 0.099942 m
  measured vertical standoff above the registered toilet-roll top.
- At execution completion, Integrated reported
  `HOLDING_AUTHORIZED_TRANSIT_ENDPOINT`.
- A later read-only status check found a subsequent platform/authority-loss
  error had marked that retained transit `RELEASED` and left Integrated
  `DEGRADED`, while Basic remained connected, healthy, gravity-supported, and
  under a renewing local lease. No command was sent during that check.
- At the time of that snapshot, the documentation did not authorize recovery
  or another physical action. The operator later supplied a separate explicit
  lift, safe-home, and shutdown authorization recorded below.

## Final bounded lift and shutdown

- The operator subsequently authorized an upward lift, safe-home, and complete
  shutdown.
- The requested 20 cm displacement was reduced by controller policy. A 13 cm
  target was the largest tested valid preview below rejected 14 cm and 15 cm
  targets.
- The measured lift was approximately 12.67 cm. The controller reached its
  eight-second one-shot deadline with about 7 mm Cartesian residual, reported
  `DEADLINE_FLOAT_BEFORE_ARRIVAL`, and confirmed gravity-float.
- Basic safe-home was confirmed.
- Test Agent, all Providers, Manager, Fabric, and arm-controller processes
  stopped, and no reviewed workspace listener remained.

## Open Cartesian-axis issue

Cartesian-axis interpretation and alignment remain challenging. The current
stationary transform maps physical vertical primarily to world `+Y` and
arm-base `+X`, but that relationship is installation- and
calibration-specific. Natural-language direction must eventually be resolved
through explicit gravity, frame, transform-revision, timestamp, and
uncertainty semantics. It must not become a hard-coded axis convention. See
`CARTESIAN_AXIS_ALIGNMENT_OPEN_ISSUE_20260729.md`.

## Publication and validation

- Synchronized root, platform-core, and Test Agent public configuration
  templates. API-key fields remain blank.
- Added Test Agent monorepo source-root setup for the Integrated Controller and
  current finite Skill packages.
- Retained the protected legacy GitHub workflow because the publishing token
  did not have the separate `workflow` OAuth scope. Its exact Python command
  passes 111 tests and skips three Agents-SDK-only modules; the full
  publication matrix remains an independently run `469/469`.
- Declared the Test Agent JSON Schema dependency and placed its SDK-only test
  import after the optional Agents SDK skip.
- Fixed the Orbbec aligned-depth validity test to load its own Provider
  entrypoint instead of the ambiguous top-level `provider` module name.
- Passed the full local stopped matrix at `578/578`, including the local-only
  cutting prototype. After removing that prototype from the clean publication
  clone, passed the exact GitHub candidate at `469/469` (439 Python and 30
  Rust tests), plus Rust formatting/release build and configuration/source
  parser checks.
