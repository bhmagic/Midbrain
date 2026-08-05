# Basic Provider validation

Run `scripts/verify.ps1` from the Provider environment for the current stopped
regression result. Do not copy an old test count into operating decisions.

Coverage includes motor-side endpoint keepalive throttling, the joint-7
`POSITION_EFFORT_LIMITED` (`POS_TOR` in Integrated)
reference-rate limiter, bounded Windows semaphore-timeout, device-command, and
register-10 confirmation retries, ordered early MIT support around mode
confirmation, dedicated physical-test `POS_VEL` and Integrated `POS_TOR` caps, payload gravity
contribution, fenced payload updates, TMAX clipping, staged motor-mode
transitions, serial-I/O telemetry, responsive cached state delivery during a
blocked control lock, lease, timeout, gravity-float, safe-home gripper-state
preservation, gripper `POSITION_EFFORT_LIMITED` to `FORCE_POS` translation, measured-speed
hysteretic brake/hold and recovery, MotorBridge, calibration, and Midbrain
integration regressions.

Python compilation, provider JSON parsing, and PowerShell syntax validation pass. This revision has not physically validated the 10 Hz motor endpoint keepalive, transient retry, first-frame bridge, expanded POS_TOR envelope, CONTACT_WORK support, payload compensation, increased allowed Kp range, or the new gripper measured-speed guard.

The file-integrity manifest covers the version-controlled Provider payload.
Provider `.venv`, active machine-local configuration, caches, generated
egg-info, and checksum files are intentionally excluded; the manifest itself
is authoritative for its current file count.
