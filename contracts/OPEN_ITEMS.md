# Open Contract Items

- Formalize clock-domain conversion, drift, offset uncertainty, and online camera/IMU time-offset estimation.
- Add BufferRef acquire/release leases and producer-death invalidation.
- Add event subscriptions and deterministic recording/replay.
- Hardware-validate the 15-state inertial-first reference backend on the Femto Bolt.
- Add a recorded-data evaluation harness with trajectory, innovation, and covariance-consistency metrics.
- Compare the reference backend against a native Basalt adapter and an OpenVINS/MSCKF evaluation build.
- Define feature-level visual update schemas if backend diagnostics are exposed beyond pose-level corrections.
- Define neck/body kinematic authority when articulated hardware exists.
- Add expiry/fencing and hard enforcement to motion-inhibit and physical Control Authority Leases.
- Define transform covariance tangent convention and conflict-selection policy beyond rejection.
