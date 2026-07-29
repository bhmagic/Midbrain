# Changelog

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
