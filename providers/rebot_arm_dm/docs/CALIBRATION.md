# Automatic friction calibration

This attended workflow estimates joint friction while retaining the factory
rigid-body model. It does not change link mass, center of mass, inertia,
gravity scale, or gravity phase. Calibration output remains machine-local and
must be reviewed before ordinary Provider startup consumes it.

Read [Safety behavior](SAFETY.md) and complete stopped validation before
opening a hardware connection. Automatic calibration uses only Basic
`POSITION_VELOCITY_LIMITED` commands (Damiao/MotorBridge `POS_VEL`); it never
uses load-bearing MIT/`IMPEDANCE` control.

## Attended workflow

1. Start Basic with hardware calibration enabled and confirm powered gravity
   support.
2. Move the arm by hand to a clear pose and capture it.
3. Select one joint and a separately reviewed, collision-approved range.
4. Start the experiment.
5. Basic places all seven motors in `POSITION_VELOCITY_LIMITED`: six hold the
   captured pose while the selected joint makes slow and fast forward/reverse
   traverses.
6. The tested joint returns to its captured angle. All seven joints remain in
   speed-limited position hold while the result is fitted and written.
7. Review and optionally save only the Coulomb and viscous-friction terms.
8. Explicitly select gravity float when ready. Calibration never chooses that
   transition on the operator's behalf.

Ranges must remain strictly inside Basic's hard limits. The calibration UI may
reduce them further using its temporary collision model, while Basic continues
to enforce absolute limits, operational limits, speed and effort caps, leases,
and deadlines.

## Fitted model

For one moving joint with the remaining joints near the captured pose, the
working model is:

`tau_measured = tau_factory_gravity(q) + F_c sign(qdot) + b qdot + tau_bias + unmodelled_terms`

The fit estimates only:

- `F_c`: symmetric Coulomb friction in N·m;
- `b`: viscous friction in N·m/(rad/s).

At approximately the same angle and speed magnitude in opposite directions:

`tau_forward = tau_g + tau_bias + F_c + b |qdot|`

`tau_reverse = tau_g + tau_bias - F_c - b |qdot|`

Therefore:

`0.5 (tau_forward - tau_reverse) = F_c + b |qdot|`

The paired difference cancels most gravity-model error and constant torque
bias. Two distinct speeds provide the intercept and slope. Samples near
acceleration, reversal, and settling are excluded; position bins pair opposite
directions at similar angles, and robust weighted least squares reduces the
effect of isolated outliers.

The resulting friction values are stored in the arm calibration and included
in Basic's `robot_arm.model` Fabric observation. Gravity compensation
continues to use the unchanged factory mass model.
