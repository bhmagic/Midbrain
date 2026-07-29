# Basic 0.1.20 validation

Software regression suite: 83/83 pass from the current workspace source root.

Coverage includes motor-side endpoint keepalive throttling, the joint-7 POS_TOR
reference-rate limiter, bounded Windows semaphore-timeout, device-command, and
register-10 confirmation retries, ordered early MIT support around mode
confirmation, dedicated physical-test POS_VEL and POS_TOR caps, payload gravity
contribution, fenced payload updates, TMAX clipping, staged motor-mode
transitions, serial-I/O telemetry, responsive cached state delivery during a
blocked control lock, lease, timeout, gravity-float, safe-home gripper-state
preservation, gripper FORCE_POS physical-ceiling translation, measured-speed
hysteretic brake/hold and recovery, MotorBridge, calibration, and Midbrain
integration regressions.

Python compilation, provider JSON parsing, and PowerShell syntax validation pass. This revision has not physically validated the 10 Hz motor endpoint keepalive, transient retry, first-frame bridge, expanded POS_TOR envelope, CONTACT_WORK support, payload compensation, increased allowed Kp range, or the new gripper measured-speed guard.

The complete provider manifest covers and verifies 53 non-runtime files.
Provider `.venv`, active machine-local configuration, caches, generated
egg-info, and checksum files are intentionally excluded.
