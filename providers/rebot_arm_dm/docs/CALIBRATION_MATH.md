# Automatic friction calibration mathematics

Automatic calibration intentionally retains the factory rigid-body model. It does not change link mass, center of mass, inertia tensor, gravity scale, or gravity phase.

For one moving joint with the remaining joints near the captured pose, the working model is:

`tau_measured = tau_factory_gravity(q) + F_c sign(qdot) + b qdot + tau_bias + unmodelled_terms`

The calibration estimates only:

- `F_c`: symmetric Coulomb friction in N·m
- `b`: viscous friction in N·m/(rad/s)

For measurements at approximately the same angle and speed magnitude in opposite directions:

`tau_forward = tau_g + tau_bias + F_c + b |qdot|`

`tau_reverse = tau_g + tau_bias - F_c - b |qdot|`

Taking half the difference gives:

`0.5 (tau_forward - tau_reverse) = F_c + b |qdot|`

This pairing cancels most gravity-model error and constant torque bias. Two distinct speeds provide the intercept and slope needed to estimate `F_c` and `b`.

Samples near acceleration, reversal, and settling are excluded from the fit. Position bins pair forward and reverse measurements at similar angles. Robust weighted least squares reduces the influence of isolated outliers.

The resulting values are stored in the arm calibration and included in the Basic Controller's `robot_arm.model` Fabric observation. Gravity compensation continues to use the unchanged factory mass model.
