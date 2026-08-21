# Motor command overwrite semantics

## Canonical names and aliases

Basic accepts the schema terms `IMPEDANCE`, `POSITION_VELOCITY_LIMITED`,
`VELOCITY`, and `POSITION_EFFORT_LIMITED`. Damiao/MotorBridge calls those
modes MIT, `POS_VEL`, `VEL`, and `FORCE_POS`. The Integrated Provider calls its corresponding profiles
`PRESS_MIT`, `TRANSIT_SPEED`, and `CONTACT_WORK`; its UI and internal policy
often abbreviate the last backend as `POS_TOR`.

Use `POSITION_EFFORT_LIMITED` in Basic API requests. `POS_TOR` is not a Basic
schema value, and `FORCE_POS` is the motor-adapter name rather than a separate
additional mode. Likewise, `POS_SPEED` describes an Integrated speed-cap policy;
it is not interchangeable with the `POS_VEL` device mode.

Basic Controller intentionally stores one pending command envelope. Every newly accepted envelope replaces the previous pending envelope. The latest envelope is reapplied by the Basic control loop until it expires, is replaced, or a safety transition clears it.

This rule has different consequences for each Damiao motor mode:

- MIT is a cyclic setpoint protocol. Basic maintains a persistent, rate-limited moving target and therefore tolerates skipped or replaced upstream setpoints comparatively well.
- POS_VEL sends only a target position and velocity limit. The motor has no application-visible trajectory queue or waypoint sequence. A new POS_VEL frame replaces the motor-side destination.
- `POSITION_EFFORT_LIMITED` (`FORCE_POS` at the motor adapter and commonly
  `POS_TOR` in Integrated) likewise sends a target position, velocity limit,
  and torque ceiling in N·m without a trajectory queue. Basic converts the SI
  ceiling to MotorBridge's ratio only at the hardware adapter boundary.

Therefore Integrated must not apply its high-rate MIT intermediate-waypoint
stream to either endpoint mode. These modes use a latched endpoint command that
is resent unchanged as a keepalive. Complex paths advance to another endpoint
only at a deliberate segment boundary after arrival or an explicitly designed
blend condition.

Basic enforces this distinction at the motor bus. MIT and gravity support
remain cyclic at the 50 Hz controller rate, but unchanged `POS_VEL` and
`FORCE_POS` endpoints are emitted at 10 Hz. A changed endpoint bypasses the
interval and is sent immediately. This prevents a 50 Hz Basic loop from
defeating Integrated's endpoint-latch semantics.

The Damiao mode-switch operation clears the motor's command values. During an explicit endpoint-to-MIT transition, Basic refreshes the old-mode hold immediately before switching one joint. It writes CTRL_MODE, places a load-supporting MIT frame directly after that ordered write, reads the register back, and then sends the normal confirmed MIT frame. MotorBridge-compatible fallbacks without public register access instead duplicate the first post-confirmation frame. This cannot make the register transition atomic, so TRANSIT_SPEED remains a physical experiment rather than the preferred working backend.

Basic publishes mode-specific operational velocity boundaries under `command_limits`. The current IMPEDANCE, POS_VEL, and POS_TOR working caps are 4.0 rad/s for all seven joints. The J1-J3 value is 80% of the official reBot application limit of 5.0 rad/s. The developmental J4-J6 and gripper value exceeds the official application `vlim` of 3.0 rad/s but remains below the configured 10.0 rad/s motor envelope. Gripper POS_TOR retains a 0.75 native translation, so its 4.0 rad/s physical request becomes a 3.0 rad/s native motor ceiling while the measured-speed brake remains referenced to the physical request. Every working cap remains bounded by the motor's configured VMAX. The separate 5.0/10.0 rad/s motor envelope is not a qualified continuous-duty whole-arm speed and is not the higher-provider command limit. Requested intent above 10 rad/s on any arm joint requires explicit authentication and intent at or above 20 rad/s is rejected before execution. These policy thresholds do not raise the effective Basic command cap.

Basic changes at most one joint's motor mode per control tick. It refreshes all other joint holds before attempting the register confirmation, captures a fixed transition reference, and withholds endpoint motion until every requested mode is confirmed. A failed confirmation invalidates the hardware mode cache so float recovery cannot send under a stale mode assumption.

Basic exposes the overwrite counters and command IDs in `command_ingress`
telemetry. A high replacement count is normal for streamed MIT control, but is
a design warning for either latched endpoint mode.

The physical internal loop defaults to 50 Hz for the next operator test. This changes both feedback polling and retransmission of the seven active joint commands; changing only the upstream Integrated stream would not reduce motor-side traffic. `hardware_io` telemetry reports observed feedback and command frame rates plus I/O failures.

Primary implementation references:

- MotorBridge Damiao `send_cmd_pos_vel` sends one position/velocity-limit frame: <https://github.com/tianrking/motorbridge/blob/main/motor_vendors/damiao/src/motor.rs>
- The official DM serial control-mode example repeatedly sends the same selected target: <https://github.com/tianrking/motorbridge/blob/main/bindings/python/examples/dm_serial_02_control_modes_demo.py>
