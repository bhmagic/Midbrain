# Open Contract Items

- Formalize clock-domain conversion, drift, offset uncertainty, and online camera/IMU time-offset estimation.
- Replace consumer-independent `freshness_ms` enforcement with passive temporal
  evidence and Skill-owned, versioned temporal policies.
- Require timestamped Fabric observations for semantic scenes, controller
  previews, authorization decisions, execution outcomes, operation progress,
  and lease/fencing state without putting Fabric in the motor-control loop.
- Add BufferRef acquire/release leases and producer-death invalidation.
- Add event subscriptions and deterministic recording/replay.
- Hardware-validate the 15-state inertial-first reference backend on the Femto Bolt.
- Add a recorded-data evaluation harness with trajectory, innovation, and covariance-consistency metrics.
- Compare the reference backend against a native Basalt adapter and an OpenVINS/MSCKF evaluation build.
- Define feature-level visual update schemas if backend diagnostics are exposed beyond pose-level corrections.
- Define neck/body kinematic authority when articulated hardware exists.
- Add expiry/fencing and hard enforcement to motion-inhibit and physical Control Authority Leases.
- Hardware-validate policy-aware graceful-stop timeout behavior, safety-critical process preservation, and explicit complete process-tree force termination.
- Define transform covariance tangent convention and conflict-selection policy beyond rejection.
