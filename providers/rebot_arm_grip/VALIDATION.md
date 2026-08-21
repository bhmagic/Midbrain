# Validation

Version 0.2.6 is software-tested only. Unit tests cover the 85 C temperature gate,
functional-open semantics, velocity-capped 50 Hz MIT position transitions and
holds, serialized MIT-to-position-effort mode-guard priming, idempotent HOT
activation with an active gripper lease, stable torque-only contact inference, carry confirmation,
all-joint mode auditing, release, and the timed MIT-float transition. Physical
qualification is still required for the functional-open threshold, contact
thresholds, the owner-requested 4 rad/s speed, torque defaults, object retention,
and thermal behavior.
