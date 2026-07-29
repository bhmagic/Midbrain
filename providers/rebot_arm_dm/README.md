# reBot Arm DM Basic Controller 0.1.20

Basic is the hardware-facing reBot/Damiao provider. It owns MotorBridge transport, seven-motor feedback, motor-mode validation, fenced operational leases, command/lease timeouts, gravity-float, calibration/friction support, and safe-home termination.

Basic owns a provider-local Python environment at `providers/rebot_arm_dm/.venv`. Create it with `scripts/setup.ps1 -WithMotorBridge`; it is not shared with Integrated and is not committed to Git.

The setup script creates missing active model, calibration, and calibration-collision files from `config_templates`. Files under the active `config` directory are machine-local and are not committed.

0.1.20 preserves the 0.1.19 architecture and adds two narrow capabilities needed by Integrated 0.7.0:

- fenced payload mass and tool-frame COM configuration under the current operational lease;
- payload gravity compensation added to MIT, gravity-float, and safe-home support.

Safe-home now revokes the active operational lease and clears pending commands
before sending its first supported MIT frame. New operational commands are
rejected while the controller is in `SAFE_HOME`, preventing late Integrated
gripper keepalives or trajectory commands from competing with homing.
Operational lease acquisition, renewal, and payload updates are also blocked
for the full safe-home operation. An explicit gravity-float safety request
cancels the safe-home writer before changing state, so the two control paths
cannot transmit concurrently.

The supplied Manager registration sets `force_kill_on_stop_timeout` to `false`.
With a Manager that supports this field, an incomplete graceful stop is
reported instead of escalating automatically to process-tree termination.
An explicitly requested force kill remains available for emergency recovery.

Combined gravity feed-forward is clipped to each motor's configured TMAX. Existing MIT tracking-effort limits, Kd limits, joint/rate limits, minimum load-bearing Kp, lease fencing, and timeout-to-float behavior remain active.

For gripper FORCE_POS commands, the requested `velocity_limit_rad_s` is a
physical ceiling rather than a raw hardware-native value. Basic applies the
configured native conversion scale and a measured-feedback hysteretic
brake/hold at that ceiling. Runtime telemetry exposes requested/native limits,
measured and peak speed, hold position, and trip history. This guard has
software coverage but still requires an authorized physical calibration before
contact use.

The arm-joint `provider_test_caps.max_kp` values now permit up to the documented MIT protocol ceiling of 500. This does not mean 500 is physically validated on this arm; it only removes the former validation conflict so Integrated can request stronger stiffness and show the effective clamp explicitly. Physical tuning should increase gradually from the known 1.0x profile.

Basic exposes motor primitives and safety functions rather than claiming high-level path maturity. The Integrated provider publishes the reviewed upstream motion profiles: usable MIT one-shot/continuous, limited unloaded POS_VEL one-shot, and no advertised capability for the experimental continuous POS_VEL or arm POS_TOR one-shot modes.
