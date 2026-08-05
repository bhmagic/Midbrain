# Changelog

## Unreleased

- Set the requested installed-arm POS_SPEED/POS_VEL cap vector to 5.0 rad/s
  for J1-J3 and 10.0 rad/s for J4-J6 and the gripper. The former is below the
  DM-J4340P 24 V no-load characteristic speed; the latter is below the
  DM-J4310 rated characteristic speed. Conservative MIT and calibration
  limits remain unchanged. Integrated still authenticates requested
  per-joint intent above 10 rad/s and rejects intent at or above 20 rad/s.

## 0.1.20 - 2026-07-29

- Treats a gripper FORCE_POS `velocity_limit_rad_s` request as a physical
  ceiling, translates it to the hardware-native velocity field with an
  explicit conservative scale, and applies a measured-feedback hysteretic
  brake/hold until speed falls below the resume threshold.
- Exposes gripper speed-guard requested/native limits, measured and peak speed,
  hold position, trip count, and last-trip telemetry.
- Treats missing register-10 mode confirmation as a bounded transient serial error and retries up to twice with a 10 ms gap before declaring the motor mode unknown.
- Adds a dedicated physical-test POS_TOR ratio ceiling of 1.0 for J1-J6 and 0.2 for the gripper. Arm requests may now use the configured motor peak envelope (27 Nm on J1-J3 and 7 Nm on J4-J6), while each command still carries the smaller ratio calculated from baseline plus the operator's external-force budget.
- Retries exact Windows serial `device does not recognize the command` / `os error 22` and semaphore-timeout failures up to twice with the same bounded 10 ms retry.
- Uses MotorBridge's public register API to place one supporting MIT frame immediately after the CTRL_MODE write and before waiting for mode read-back, then sends the normal confirmed frame. This is a final bridge experiment for the protocol behavior that clears command values during the POS_VEL-to-MIT transition; normal MIT streaming remains unchanged.
- Sends unchanged POS_VEL/POS_TOR endpoints to the motor bus at 10 Hz while retaining 50 Hz MIT load support; a changed endpoint is sent immediately.
- Exposes serial retry/recovery/failure telemetry. A persistent timeout after the bounded retries still faults.
- Refreshes the selected POS_VEL/POS_TOR hold immediately before each explicit transition into gravity-supported MIT.
- Adds dedicated physical-test POS_VEL caps of 2.0 rad/s for J1-J3, 2.5 rad/s for J4-J6, and 0.5 rad/s for the gripper. These remain below the configured motor VMAX values and do not raise the MIT or calibration velocity caps.
- Validates POS_VEL endpoint envelopes against those dedicated caps so a valid fast endpoint is not rejected by the deliberately conservative MIT/calibration rate limits.
- Reduces the physical motor loop from 60 Hz to 50 Hz after a repeated Windows serial semaphore timeout at 60 Hz.
- Serves a bounded cached controller snapshot when serial I/O occupies the control lock, keeping health and shutdown diagnostics reachable.
- Records and prints control-fault counts, timestamps, fallback failures, and hardware-I/O counters.
- Stages motor-side mode changes one joint per control tick and refreshes every other joint hold before the register confirmation. Endpoint motion begins only after all requested modes are confirmed.
- Exposes active modes, pending float transitions, mode-switch attempts/failures, and transition telemetry.
- Physically exercised gripper motion, lease transfer, controller handover,
  upstream-loss behavior, bounded Cartesian raises, and return-to-safe-home
  behavior in the padded guarded workcell. No uncontrolled drop was observed
  during the completed handovers.
- Preserved powered endpoint support through normal non-error handovers. Error
  handling continues to prefer the existing gravity-supported fallback.
  Safe-home preserves the measured gripper angle instead of forcing a clamp.
- Confirmed the final Basic safe-home after the Agent SDK checkpoint and
  bounded shutdown lift. The authoritative helper stopped the Basic process
  only after safe-home success.

- Preserves the 0.1.19 Basic hardware/safety architecture.
- Adds fenced payload mass/tool-frame COM API under the operational lease.
- Adds payload gravity torque to MIT, gravity-float, and safe-home support.
- Clips combined gravity feed-forward to configured motor TMAX.
- Raises arm-joint MIT max-Kp validation caps to the recorded protocol ceiling of 500 while retaining existing minimum Kp, Kd, torque, tracking-effort, lease, timeout, gravity-float, and safe-home protections.
- Revokes and fences the active operational lease before safe-home sends its
  first supported MIT frame.
- Rejects late operational commands while the controller is in `SAFE_HOME`.
- Disables automatic Manager force termination for the supplied Basic provider
  registration so a graceful-stop timeout preserves powered support.
