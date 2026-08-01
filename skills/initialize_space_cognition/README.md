# Initialize / Re-establish Space Cognition

This finite Skill establishes Midbrain's local spatial origin from a stationary
camera/robot pose. It is automatically invoked in non-destructive
`initialize-if-needed` mode by the Test Agent application at startup and is
also exposed as the approval-gated `reinitialize_space_cognition` Agent tool.

Relative-motion preview also has a non-destructive
`VERIFY_EXISTING_EPOCH` readiness path. It is allowed only after the operator
confirms that the camera and IMU form a rigidly fixed rig that can remain
stationary during the check. This path may start the camera and VIO Providers
and supplies a bounded, VIO-local fixed-rig stationary attestation while
waiting for `TRACKING`. It does not acquire the global motion inhibit, send a
VIO reset, change the session epoch, revoke an arm-controller lease, revoke
workcell calibration, or clear observations. It is used to prepare
gravity-aligned before/after visual motion evidence.

Its browser development surface is
`http://127.0.0.1:8000/dev/skills/initialize-space-cognition`. The page owns
initialization and reset controls, detailed VIO state, and accumulated
world-point-cloud inspection. The general Developer Agent keeps a read-only
copy of the world point cloud and links to this Skill-owned surface.

A deliberate re-initialization is an epoch transition, not a Provider restart.
Before the Local VIO Provider receives `force_reset`, the Skill revokes every
active workcell-calibration activation. The Test Agent host then clears its
epoch-bound world point accumulation and follows the newly reported
`session_epoch` and `world_frame`.

Downstream spatial Skills must carry the session epoch through capture,
transform lookup, calibration, and execution. Data from a previous epoch may
remain available for diagnostics, but it is historical and must never be
treated as motion-usable in the new epoch.
