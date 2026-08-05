# Changelog

This file records release-level outcomes, not every development step.
Historical entries may use the names current at the time. Use the
[current terminology map](README.md#control-terminology), `manifest.json`, and
the live capability response for implementation work.

## Unreleased

- Made `TRANSIT_SPEED` the ordinary-motion default and added
  `robot.motion.arm.integrated.pos_vel.one_shot`; retained the former
  `_limited` name as a deprecated alias.
- Replaced small Cartesian and aggregate-travel proxies with the 1.2 m request
  envelope plus actual IK, calibrated joint, singularity, semantic-scene, and
  motor/provider speed limits. Requests above the policy threshold require
  authentication or are rejected before execution.
- Added bounded relative controlled-frame deltas and support for
  `MOUNTED_CANONICAL_CAMERA_CALIBRATION_GATED_V2`, while retaining the
  identity/VIO-gated V1 compatibility policy.
- Revalidated every stored waypoint against the newest accepted semantic
  scene at commit instead of requiring one frozen scene revision.
- Added preview-bound `FLOAT`, `FIXED`, and bounded `WAIT_FOR_NEXT` terminal
  states for consecutive signed paths.
- Added a bounded higher-stiffness terminal-settling phase for one-shot
  impedance motion while retaining measured arrival and fallback checks.

## 0.8.3 - 2026-08-03

- Executed signed controller-owned transit waypoints through rate-limited
  impedance while Basic retained gravity-feed-forward authority.
- Allowed a transit preview to request 6-DoF IK without mutating the global
  operator mode.

## 0.8.2 - 2026-08-03

- Added leased gravity-float, compliant-hold, and position-lock idle profiles
  with measured endpoint capture and return to gravity float on release,
  expiry, failure, or motion supersession.

## 0.8.1 - 2026-07-31

- Corrected the configured joint-3 single-commit envelope needed for the
  reviewed upward safe-home test and exposed per-joint rejection diagnostics.

## 0.8.0 - 2026-07-29

- Added controller-owned nonphysical transit planning and exact, short-lived,
  one-time signed physical commit with commit-time identity, lease, inhibit,
  scene, collision, and measured-start revalidation.
- Added adaptive waypoint continuity, final endpoint hold/release, local exact
  control auditing, and asynchronous Fabric audit publication.
- Added observation-only Manager-authority versus Integrated-writer versus
  Basic-lease comparison with separate fencing namespaces.
- Completed one guarded OpenAI Agents SDK transit through the signed boundary;
  later authority loss correctly returned the arm to Basic gravity support.

## 0.7.0

- Established the current Basic/Integrated split, Provider-local environments,
  fenced Basic leasing, payload forwarding, measured/commanded telemetry, and
  authoritative safe termination.
- Added 3-DoF/6-DoF IK, controlled-frame offsets, Fabric target staging,
  capability readiness, nonphysical preview, and operator-gated MIT and
  latched endpoint profiles.
- Added attended gripper MIT/effort-limited tests and experimental
  CONTACT_WORK with a separate gravity-float baseline and explicit effort
  budgets. Experimental profiles remained outside Agent discovery.
- Added endpoint keepalive/mode-transition coordination with Basic, explicit
  completion-versus-arrival reporting, and return-to-float behavior.
