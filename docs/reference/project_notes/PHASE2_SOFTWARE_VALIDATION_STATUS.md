# Phase 2 Systemic Housecleaning — Software Validation Status

Date: 2026-07-27
Status: software-complete in non-enforcing/shadow mode; Phase 2 physical gate
completed

## Safety and scope

The final software pass did not start a provider, camera, VLM backend, or
robot. The separate user-attended physical gate was subsequently completed and
is recorded in `PHASE2_PHYSICAL_VALIDATION_REPORT.md`.

Manager authority and Manager shutdown remain observational/shadow features.
The Basic arm provider's local fenced lease, watchdog, gravity-float behavior,
safe-home sequence, and the Integrated controller's upstream-loss handling
remain the load-bearing safety mechanisms.

The frozen reference remains
`.reference_baselines/pre_phase1_20260726`. A source/configuration scan confirms
that runtime code does not import, execute, or otherwise reference it.

## Implemented separations

### Agent discovery and binding

- Seven Skill discovery descriptors validate against
  `agent_skill_discovery.v1`.
- The local catalog reads manifests without importing or starting Skills.
- Only explicit allowlisted adapters become OpenAI Agents SDK function tools.
- The initial Test Agent requires a selected Skill and allows only one
  invocation at a time.
- Manager capability binding is preferred when available; explicit provider
  IDs remain deterministic compatibility fallbacks.
- Provider instance, boot, route, and binding provenance is retained.

### RGB-D route and spatial registration

- Orbbec publishes a generic shared-memory RGB-D route and the branded direct
  fallback route at the same time.
- The generic descriptor supports independent RGB, IR, native-depth, and
  registered-depth dimensions, aspect ratios, valid regions, calibration,
  timestamps, and provider-written custom alignment.
- Bulk image/depth data stays in shared memory. Fabric carries references,
  timestamps, geometry, alignment, and other small metadata.
- `spatial_registration_rgbd` owns pixel/depth/world registration and supports
  robust-median, closest-depth, and nearest-valid-pixel selection.

### VLM and reflective-tool registration

- `visual_scene_analysis` provides a general finite VLM route with ordered
  backend fallback and backend provenance.
- Voting and quality-control ensembles remain explicitly disabled and
  documented as future guarded-deployment work.
- `register_tool_to_control_frame` combines agentic VLM landmarks with spatial
  registration and closest-depth selection for reflective blades.
- Tool-frame output is review-only and motion-unusable until separately
  accepted and published.

### Stationary workcell calibration

- `calibrate_stationary_workcell` is the discoverable finite Skill exposed by
  the `stationary_world_arm_alignment` package.
- FoundationPose now has a finite Skill-local engine route as well as the
  compatibility provider route.
- Automatic fallback between those routes is disabled until a guarded
  hardware comparison is complete.
- The Skill-local route consumes fresh RGB-D frames, records the matching VIO
  transform per frame, rejects a VIO epoch change, and does not create a
  resident FoundationPose provider session.

### Controller planning, audit, and shutdown

- The Integrated controller owns shadow path preview, speed limits, and
  singularity-escape candidates.
- Vegetable cutting requests a controller preview but keeps its legacy
  interpolation path during the comparison period.
- Every submitted controller request is copied locally before action with a
  canonical request hash and lifecycle events.
- Fabric publication is asynchronous, so Fabric delay is outside the direct
  control path.
- Manager computes shutdown ordering in shadow mode and preserves
  safety-critical process ordering.

### Browser UI and authorization

- Ordinary UI chrome is neutral dark white/grey/black across the Integrated
  controller, Basic arm calibration, Orbbec calibration, stationary
  calibration, vegetable cutting, and Test Agent browser surfaces.
- Color remains only where it conveys safety/status meaning, image annotation,
  coordinate axes, or distinct data series.
- The legacy FoundationPose development window is also neutralized while its
  future browser conversion remains a compatibility task.
- Authorization is per decision and popup-compatible. Approval records a
  decision only; it cannot execute motion or bypass provider safety checks.

### Test Agent and deferred slicing

- `observe_pointed_object` proposes a front or top end-effector observation
  pose, requests controller path preview, and creates an authorization record.
- The proposal is nonphysical and is not in the initial agent's execution
  allowlist.
- Axial/sawing slice behavior is separated into a future Skill plan. The future
  Skill owns cutting intent; the controller owns trajectory quality, speed,
  singularity, and collision policy.

## Software validation

After closing the defects found during physical testing, the final
nonphysical validation result is 414 passing tests:

| Component | Passing tests |
| --- | ---: |
| Integrated arm controller | 69 |
| Basic arm provider | 81 |
| Orbbec provider | 6 |
| Local VIO | 30 |
| FoundationPose compatibility package | 43 |
| Manager and Fabric platform core | 14 |
| Test Agent, discovery, authorization, routing, and UI contracts | 26 |
| Vegetable-cutting local prototype | 106 |
| Stationary workcell calibration | 32 |
| Generic spatial registration | 4 |
| Tool-to-control-frame registration | 3 |
| **Total** | **414** |

Additional checks passed:

- all 13 JSON theme/Skill/provider manifests parse;
- all seven discoverable Skill descriptors pass the discovery JSON schema;
- changed Python trees compile;
- no runtime or configuration source references the frozen baseline;
- no UI preview process remains listening after validation.

## Completed physical-control gate

The user-attended Phase 2 gate completed safe-home, guarded free-space
movement, gripper inspection, mode transition, lease release/recovery,
upstream-loss emulation, final safe-home, and shutdown.

The resulting defects were corrected and regression-tested before Phase 3.
The initial Phase 3 physical closure scope was:

1. inspect current provider, lease, and arm state;
2. start the reviewed stack and run safe-home;
3. move only `(0, 0, +0.02 m)` for the authorized lease-validity loop;
4. verify no WARM-release misclassification or background reacquire;
5. return through safe-home and stop the arm software.

That guarded loop is complete and recorded in
`PHASE3_GATE0_PHYSICAL_VALIDATION_REPORT.md`.

No enforcement policy is eligible to turn on merely because the software tests
pass. Each interconnection must first be observed in shadow mode, then switched
individually under the guarded hardware procedure, and finally regression
tested again.
