# Basic 0.1.20 validation

Software regression suite: 73/73 pass in the current provider venv.

Coverage includes motor-side endpoint keepalive throttling, bounded Windows semaphore-timeout, device-command, and register-10 confirmation retries, ordered early MIT support around mode confirmation, dedicated physical-test POS_VEL and POS_TOR caps, payload gravity contribution, fenced payload updates, TMAX clipping, staged motor-mode transitions, serial-I/O telemetry, responsive cached state delivery during a blocked control lock, lease, timeout, gravity-float, safe-home, MotorBridge, calibration, and Midbrain integration regressions.

Python compilation, provider JSON parsing, and PowerShell syntax validation pass. This revision has not physically validated the 10 Hz motor endpoint keepalive, transient retry, first-frame bridge, expanded POS_TOR envelope, CONTACT_WORK support, payload compensation, or increased allowed Kp range.

The complete provider manifest covers and verifies 56 non-runtime files. Provider `.venv`, caches, generated egg-info, and checksum files are intentionally excluded.
