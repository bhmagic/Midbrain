# Initialize / Re-establish Space Cognition

This finite Skill establishes Midbrain's local spatial origin from a stationary
camera/robot pose. It is automatically invoked in non-destructive
`initialize-if-needed` mode by the Test Agent application at startup and is
also exposed as the approval-gated `reinitialize_space_cognition` Agent tool.

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
