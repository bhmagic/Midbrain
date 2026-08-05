# Architecture

## Provider boundary

The Basic Controller is the sole owner of the motor device. It contains no desktop geometry, self-collision model, general obstacle map, or MoveIt dependency. Its local safety boundary consists of command freshness, lease fencing, joint limits, speed/torque caps, motor health, powered support, and safe-home.

The calibration application is a separate client. It owns the temporary desktop plane, simplified collision bodies, calibration trajectories, and user workspace confirmation.

## Safety authority

Basic enforces load-bearing stiffness, damping, joint, rate, effort, tracking,
lease, deadline, gravity-support, and safe-home rules locally. The canonical
operator explanation and failure boundary are in [Safety behavior](SAFETY.md);
higher-level Providers must not restate or weaken those limits.

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

Automatic calibration uses only `POSITION_VELOCITY_LIMITED` and includes all
seven joints in every request, preventing an uncommanded joint from falling
back to another mode. The attended sequence and fitted model are defined in
[Automatic friction calibration](CALIBRATION.md).

## Timing

The current physical-test local loop is 50 Hz to reduce serial load after a USB write timeout was still observed at 60 Hz. MIT targets are rate-limited locally. Motor-side modes are refreshed for watchdog purposes. Fabric publication is not the real-time command path.

## Graceful-stop continuity

Normal termination is one continuous powered sequence from gravity support to
safe-home and then motor disable. The exact operator-visible invariant and the
limits of non-graceful failure handling are owned by
[Safety behavior](SAFETY.md).
