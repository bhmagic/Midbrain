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
hysteretic brake/hold and recovery, MotorBridge, the attended Hardware
Development UI, and Midbrain integration regressions.

The software suite also verifies that `POSITION_EFFORT_LIMITED` accepts
`torque_limit_nm`, rejects the former public ratio field, publishes the active
per-joint command ceilings, and converts N·m to the MotorBridge ratio only at
the Basic hardware-adapter boundary.

Python compilation, provider JSON parsing, and PowerShell syntax validation
pass. On 2026-08-11, the operator reported successful gravity balancing and
several 6-DoF free-space motions during development of the selected `5 inch
blade` assembly. That observation does not cover the exact motion envelope,
repeatability, fault injection, or every arm pose and therefore does not
physically qualify payload compensation across the operating envelope. The
10 Hz motor endpoint keepalive, transient retry, first-frame bridge, expanded
POS_TOR envelope, CONTACT_WORK support, increased allowed Kp range, and the new
gripper measured-speed guard also remain outside this limited observation.

The file-integrity manifest covers the version-controlled Provider payload.
Provider `.venv`, active machine-local configuration, caches, generated
egg-info, and checksum files are intentionally excluded; the manifest itself
is authoritative for its current file count.
