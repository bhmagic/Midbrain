# Architecture

## Provider boundary

The Basic Controller is the sole owner of the motor device. It contains no desktop geometry, self-collision model, general obstacle map, or MoveIt dependency. Its local safety boundary consists of command freshness, lease fencing, joint limits, speed/torque caps, motor health, powered support, and safe-home.

The calibration application is a separate client. It owns the temporary desktop plane, simplified collision bodies, calibration trajectories, and user workspace confirmation.

## Hard safety rule: MIT `kp`

For load-bearing MIT control, low spring stiffness is forbidden. The minimum permitted `kp` is the supplied tested default for each joint: 120 for joints 1–3, 18 for joints 4–6, and 8 for the gripper. `kd` is velocity damping and may be lower. This rule is enforced in configuration validation and command validation.

## Control states

- `DISCONNECTED`: no device.
- `READ_ONLY`: feedback and publication, no commanded motion.
- `CALIBRATION_MANUAL`: a valid calibration lease controls bounded motion.
- `CALIBRATION_POSITION_HOLD`: all joints held by motor-side `POSITION_VELOCITY_LIMITED`; used throughout automatic calibration and result processing.
- `TRAJECTORY_CONTROL`: reserved for the planning provider.
- `SAFE_HOLD_GRAVITY_FLOAT`: factory gravity torque plus high-`kp` MIT whose position target follows measured position each cycle.
- `SAFE_HOME`: powered movement to the configured home vector.
- `FAULTED`: motion blocked pending recovery.
- `EMERGENCY_DISABLED`: output disabled under emergency policy.

## Command modes

- `IMPEDANCE`: MIT position, velocity, spring stiffness `kp`, velocity damping `kd`, and feed-forward torque.
- `POSITION_VELOCITY_LIMITED`: motor-side position with velocity limit.
- `VELOCITY`: continuous velocity command; excluded from the calibration GUI.
- `POSITION_EFFORT_LIMITED`: motor-side force-limited position using target, velocity limit, and torque ratio.

## Automatic calibration boundary

Automatic calibration uses only `POSITION_VELOCITY_LIMITED`. Every request contains all seven joints so no uncommanded joint falls back to MIT during a test. Six joints hold the captured pose and the selected joint moves. The final captured-pose hold remains active during regression and file output.

## Timing

The current physical-test local loop is 50 Hz to reduce serial load after a USB write timeout was still observed at 60 Hz. MIT targets are rate-limited locally. Motor-side modes are refreshed for watchdog purposes. Fabric publication is not the real-time command path.

## Graceful-stop continuity

`SAFE_HOLD_GRAVITY_FLOAT -> SAFE_HOME -> motor disable` is one continuous powered sequence. Safe-home clamps `kp` to the same load-bearing floor used by gravity-float, captures measured position before changing the target, and keeps gravity support active through the powered settle interval. Only a second forced termination request may bypass this sequence.
