# Changelog

## Unreleased

- Keep a newly signed session alive while it is waiting for its first
  setpoint. That pre-command state owns a lease but has no active motor
  endpoint; the first move performs the required fresh-feedback check. Reset
  lease-renewal timing for each new session and service it during long
  Cartesian IK construction, which otherwise holds the controller operation
  lock. Retain the last control fault and relax reason in state and
  inactive-session errors for physical-test diagnosis.
- Decouple Basic feedback polling from the Contact Cartesian command loop.
  Both run at Basic's advertised control rate; commands consume a
  freshness-checked feedback cache instead of serializing state and command
  HTTP requests in every update.
- Consume Basic's 4.0 rad/s J1-J6 `POSITION_EFFORT_LIMITED` limits for segment
  timing without introducing a Contact-owned speed policy.
- Start each signed inactivity interval after the move's calculated
  Basic-limit-derived transition time, so a long valid segment does not consume
  the default six-second safe wait. Publish the resulting timing semantics and
  per-segment commanded-versus-measured FK tracking diagnostics.
- Add signed absolute and measured-start-relative position modes. Relative
  root-axis displacement is resolved from fresh controlled-effector FK when a
  move is accepted and is published alongside its resolved target.
- Add explicit `ONE_SHOT` and `CARTESIAN_SEGMENT` move semantics. Contact now
  derives its stream cadence from Basic's advertised internal control rate,
  decomposes a segment into sequential IK knots at no more than 2 mm spacing,
  time-parameterizes them with Basic's joint limits, and streams changing
  position-effort-limited targets at the current 50 Hz rate.
- Preserve immediate new-move replacement and final endpoint holding; no queue
  of Skill moves or arrival-success assertion is introduced.

## 0.1.0 - 2026-08-12

- Add the independent Contact Work Provider and direct Basic arm-group lease.
- Add exact Skill-signed plan authorization, immediate endpoint replacement,
  six-second inactivity relaxation, and measured joint-state publication.
- Add independent locked-joint IK and full acting-point `J^T w` mapping.
- Apply full Basic-authorized effort to locked joints and gravity plus at least
  20 percent configured maximum effort to non-locked joints.
- Add the task-specific slicing plan boundary. It uses force components only
  and emits zero rotational torque, while the Provider accepts and maps all
  six wrench components without a rotational-component gate.
- Retain the best weighted 6-DoF IK iterate under hard joint locks and report a
  measured-to-target velocity-limited transition time for Skill-side minimum
  command spacing.
- Remove joint speed from Contact plans. Consume Basic's declared per-joint
  POSITION_EFFORT_LIMITED speed limits for both commands and timing, and use
  N·m torque ceilings across the Contact-to-Basic boundary.
