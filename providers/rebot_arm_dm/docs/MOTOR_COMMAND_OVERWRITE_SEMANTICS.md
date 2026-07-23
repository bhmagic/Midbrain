# Motor command overwrite semantics

Basic Controller intentionally stores one pending command envelope. Every newly accepted envelope replaces the previous pending envelope. The latest envelope is reapplied by the Basic control loop until it expires, is replaced, or a safety transition clears it.

This rule has different consequences for each Damiao motor mode:

- MIT is a cyclic setpoint protocol. Basic maintains a persistent, rate-limited moving target and therefore tolerates skipped or replaced upstream setpoints comparatively well.
- POS_VEL sends only a target position and velocity limit. The motor has no application-visible trajectory queue or waypoint sequence. A new POS_VEL frame replaces the motor-side destination.
- FORCE_POS likewise sends a target position, velocity limit, and torque-limit ratio without a trajectory queue.

Therefore Integrated must not apply its high-rate MIT intermediate-waypoint stream to POS_VEL or FORCE_POS. These motor-side modes use a latched endpoint command that is resent unchanged as a keepalive. Complex paths advance to another endpoint only at a deliberate segment boundary after arrival or an explicitly designed blend condition.

Basic enforces this distinction at the motor bus. MIT and gravity support remain cyclic at the 50 Hz controller rate, but unchanged POS_VEL and POS_TOR endpoints are emitted at 10 Hz. A changed endpoint bypasses the interval and is sent immediately. This prevents a 50 Hz Basic loop from defeating Integrated's endpoint-latch semantics.

The Damiao mode-switch operation clears the motor's command values. During an explicit endpoint-to-MIT transition, Basic refreshes the old-mode hold immediately before switching one joint. It writes CTRL_MODE, places a load-supporting MIT frame directly after that ordered write, reads the register back, and then sends the normal confirmed MIT frame. MotorBridge-compatible fallbacks without public register access instead duplicate the first post-confirmation frame. This cannot make the register transition atomic, so TRANSIT_SPEED remains a physical experiment rather than the preferred working backend.

Physical TRANSIT_SPEED uses a separate POS_VEL cap vector from the conservative MIT/calibration rate caps. The current caps are 2.0 rad/s for J1-J3, 2.5 rad/s for J4-J6, and 0.5 rad/s for the gripper; every value remains bounded by that motor's configured VMAX. Raising these POS_VEL caps does not alter MIT target-rate validation or automatic-calibration excitation limits.

Basic changes at most one joint's motor mode per control tick. It refreshes all other joint holds before attempting the register confirmation, captures a fixed transition reference, and withholds endpoint motion until every requested mode is confirmed. A failed confirmation invalidates the hardware mode cache so float recovery cannot send under a stale mode assumption.

Basic exposes the overwrite counters and command IDs in `command_ingress` telemetry. A high replacement count is normal for streamed MIT control, but is a design warning for latched POS_VEL or FORCE_POS execution.

The physical internal loop defaults to 50 Hz for the next operator test. This changes both feedback polling and retransmission of the seven active joint commands; changing only the upstream Integrated stream would not reduce motor-side traffic. `hardware_io` telemetry reports observed feedback and command frame rates plus I/O failures.

Primary implementation references:

- MotorBridge Damiao `send_cmd_pos_vel` sends one position/velocity-limit frame: <https://github.com/tianrking/motorbridge/blob/main/motor_vendors/damiao/src/motor.rs>
- The official DM serial control-mode example repeatedly sends the same selected target: <https://github.com/tianrking/motorbridge/blob/main/bindings/python/examples/dm_serial_02_control_modes_demo.py>
