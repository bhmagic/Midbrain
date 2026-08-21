# Changelog

## 0.2.6 - 2026-08-20

- Replace joint-position-error and settled-velocity contact predicates with
  the owner-requested torque-only development predicate: absolute measured
  motor torque must remain at or above `0.15` N m for 10 consecutive 50 Hz
  samples. Position and velocity remain diagnostics.
- Raise every Rebot new-grip admission gate from `70` to `85` C while
  retaining rejection of missing, stale, and non-finite temperature feedback.

## 0.2.5 - 2026-08-20

- Raised the owner-requested attended-development open/close request ceiling
  from `0.7` to `4.0` rad/s and shortened the default signed 50 Hz MIT opening
  transition from `2.5` to `1.0` seconds.
- Retained Basic's measured-speed brake and `0.75` native FORCE_POS velocity
  translation. This value is software-tested but physically unqualified and
  exceeds the official reBot application `vlim` of `3.0` rad/s.

## 0.2.4 - 2026-08-20

- Adopt the active effector profile's owner-observed `0` degree normal-object
  firm-close endpoint. Stable contact must occur before that endpoint; soft or
  thin object behavior remains outside this control profile.

## 0.2.3 - 2026-08-20

- Adopt the active effector profile's `-10` degree normal-object close
  endpoint. Stable contact must occur before that endpoint; soft or thin
  object behavior remains outside this control profile.

## 0.2.2 - 2026-08-20

- Publish measured gripper torque in control state so contact-inference
  timeouts can report which physical predicate remains unsatisfied.

## 0.2.1 - 2026-08-20

- Serialize finite Grip commands against the 50 Hz background stream so a
  previous MIT hold cannot replace the position-effort priming command before
  Basic enables its exact mode guard.
- Make an already-HOT activation idempotent while the Provider owns its
  gripper-group lease instead of attempting to rebind the leased resource.

## 0.2.0 - 2026-08-20

- Added signed, velocity-capped 50 Hz MIT gripper position interpolation with a
  persistent endpoint hold on the independently leased gripper group.
- Defined functional openness at `-180` degrees, distinct from the physical
  `-280` degree fully open endpoint, and published explicit approach readiness.
- Corrected the grip profile to the Integrated-compatible convention: more
  negative opens, while increasing position closes toward `-20` degrees.

## 0.1.0 - 2026-08-19

- Added the independent gripper actuator-group Provider, thermal admission
  gate, position/effort carrying invariant, release, and MIT-float transition.
