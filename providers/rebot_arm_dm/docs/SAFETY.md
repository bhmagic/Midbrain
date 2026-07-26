# Safety behavior

## Critical MIT stiffness rule

**Low `kp` is prohibited for every load-bearing MIT state.** `kp` is the spring constant and must remain at or above the provided tested value for each motor group: joints 1–3 use 120, joints 4–6 use 18, and the gripper uses 8. A low `kp` can provide insufficient restoring torque and allow the arm to fall before gravity support stabilizes.

`kd` is velocity damping. It may be comparatively low when required for compliant motion, provided it is finite, nonnegative, and within the reviewed motor limit. Do not confuse low damping with low spring stiffness.

The Basic Controller enforces the minimum `kp` at configuration load and command validation. This is a hard runtime rule, not an operator warning.

## Gravity-float safe hold

`SAFE_HOLD_GRAVITY_FLOAT` uses factory gravity feed-forward plus the provided high `kp`. For arm joints, the MIT position target is refreshed to the current measured angle every control cycle. The high spring constant gives immediate load support, while the continuously refreshed target prevents a persistent rigid positional lock. Damping may remain comparatively low.

Command expiration, GUI loss, lease expiry, and normal manual cancellation transition to this state. If feedback or the gravity model is invalid, the provider cannot promise gravity support and must fault or use the best available restricted response.

## Automatic calibration

Automatic friction calibration never uses MIT. All seven motors are commanded in `POSITION_VELOCITY_LIMITED`: six hold the captured pose and one performs the test sweep. After motion and while fitting or writing results, all seven remain in speed-limited position hold. The operator explicitly chooses when to transition back to gravity-float.

## Graceful termination

A normal stop fences the active operational lease, clears pending commands,
rejects new commands while `SAFE_HOME` is active, executes the configured
safe-home movement, verifies tolerance, then disables motors and releases the
device. If homing fails, powered support is retained when possible instead of
deliberately removing support.

The supplied Manager registration disables automatic force termination after a
graceful-stop timeout. The Manager reports the timeout and leaves the Basic
process running so powered support is not silently removed. An operator can
still request the explicit force-kill endpoint when the physical situation
requires it.

## Non-graceful failure

A process crash, complete power loss, USB bridge loss, or some motor faults may make powered support and safe-home impossible. Mechanical counterbalance, brakes, backup power, or an independent low-level safety controller are needed to cover those cases.

## Calibration motion

The GUI excludes raw velocity mode. User ranges must be strictly inside Basic Controller hard limits. The GUI may reduce ranges further using its temporary collision model. The Basic Controller independently enforces absolute and configured operational limits, speed caps, MIT `kp` floors, torque caps, and deadlines.

## Seamless safe-home transition

Safe-home is a load-bearing MIT state. The first safe-home command must capture the latest measured pose using the full reviewed `kp` floor and gravity feed-forward before the target begins moving. The controller holds that supported pose for several control cycles to complete any motor-mode transition. During graceful shutdown, powered gravity support remains active until immediately before motor disable. File writes, HTTP shutdown, and thread joins must not occur inside an unsupported transition interval.
