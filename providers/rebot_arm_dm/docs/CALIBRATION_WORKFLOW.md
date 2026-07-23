# Automatic friction calibration workflow

1. Start the Basic Controller with hardware calibration enabled.
2. Confirm powered gravity support.
3. Move the arm by hand to a clear pose and capture it.
4. Select a joint and a collision-approved range.
5. Start the experiment.
6. The controller switches all seven motors to `POSITION_VELOCITY_LIMITED`.
7. Six joints hold the captured pose; one joint performs slow and fast forward/reverse traverses.
8. The tested joint returns to its captured angle.
9. All seven joints remain in speed-limited position hold while fitting and writing the concise result.
10. Review and optionally save only Coulomb and viscous friction. Factory mass and gravity remain unchanged.
11. Explicitly select Gravity Float when ready; automatic calibration never transitions itself through low-gain MIT.

Low `kp` MIT is prohibited for load-bearing control. `kp` is spring stiffness; `kd` is velocity damping and may be comparatively low.
