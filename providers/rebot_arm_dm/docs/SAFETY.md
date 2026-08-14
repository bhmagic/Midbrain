# Safety behavior

## Critical MIT stiffness rule

**Low `kp` is prohibited for every load-bearing MIT state.** `kp` is the spring constant and must remain at or above the provided tested value for each motor group: joints 1–3 use 120, joints 4–6 use 18, and the gripper uses 8. A low `kp` can provide insufficient restoring torque and allow the arm to fall before gravity support stabilizes.

`kd` is velocity damping. It may be comparatively low when required for compliant motion, provided it is finite, nonnegative, and within the reviewed motor limit. Do not confuse low damping with low spring stiffness.

The Basic Controller enforces the minimum `kp` at configuration load and command validation. This is a hard runtime rule, not an operator warning.

## Gravity-float safe hold

`SAFE_HOLD_GRAVITY_FLOAT` uses factory gravity feed-forward plus the provided high `kp`. For arm joints, the MIT position target is refreshed to the current measured angle every control cycle. The high spring constant gives immediate load support, while the continuously refreshed target prevents a persistent rigid positional lock. Damping may remain comparatively low.

Command expiration, GUI loss, lease expiry, and normal manual cancellation transition to this state. If feedback or the gravity model is invalid, the provider cannot promise gravity support and must fault or use the best available restricted response.

## Graceful termination

A normal stop fences the active operational lease, clears pending commands,
rejects new commands while `SAFE_HOME` is active, executes the configured
safe-home movement, verifies tolerance, then disables motors and releases the
device. If homing fails, powered support is retained when possible instead of
deliberately removing support.

A later explicit stop request is also the recovery path for a failed first
attempt. It does not require the arm to be near the configured home position.
Before issuing another protective trajectory, Basic observes every installed
joint over the configured stationary-shutdown window. The observation requires
fresh, advancing, generation-verified feedback and bounds both measured
position span and measured velocity. If every joint remains stationary, Basic
permits termination with `MEASURED_STATIONARY_RETRY`, disables the motors,
releases the device, and exits. Absolute joint position is diagnostic only for
this retry result.

If the controller is already `FAULTED` or `EMERGENCY_DISABLED` and a repeated
stop cannot confirm stationary feedback, Basic permits process release with
`CONTROL_UNAVAILABLE_RETRY`. That result explicitly sets
`physical_outcome_known=false`: it does not claim that the arm is safe, only
that retaining the failed process cannot provide active support. This path is
never available on the first stop attempt and never restores motion authority.

If fresh feedback shows movement and the controller remains available, the
retry continues through the bounded safe-home path. A second failure remains
visible and may be retried again; shutdown no longer creates an unresolvable
position-only gate.

The supplied Manager registration disables automatic force termination after a
first graceful-stop timeout. The Manager reports the timeout and leaves the
Basic process running so powered support is not silently removed. A later
explicit shutdown retries the Provider-owned termination decision. An operator
can still request the explicit force-kill endpoint when the physical situation
requires it.

## Non-graceful failure

A process crash, complete power loss, USB bridge loss, or some motor faults may make powered support and safe-home impossible. Mechanical counterbalance, brakes, backup power, or an independent low-level safety controller are needed to cover those cases.

## Hardware-development motion

The attended UI excludes raw velocity mode and automatic calibration motion.
Each manual slider is a pointer deadman; releasing it requests gravity float.
User test ranges must remain strictly inside Basic Controller hard limits. The
UI's temporary collision model reports only the current measured pose and does
not approve a range. Basic independently enforces leases, deadlines, absolute
and configured operational limits, speed caps, MIT `kp` floors, and torque
caps.

## Seamless safe-home transition

Safe-home is a load-bearing MIT state. The first safe-home command must capture the latest measured pose using the full reviewed `kp` floor and gravity feed-forward before the target begins moving. The controller holds that supported pose for several control cycles to complete any motor-mode transition. During graceful shutdown, powered gravity support remains active until immediately before motor disable. File writes, HTTP shutdown, and thread joins must not occur inside an unsupported transition interval.
