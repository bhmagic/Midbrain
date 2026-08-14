# Contact Work safety behavior

This Provider is development software for deliberate contact, not a certified
safety function. Basic hard limits, operational limits, fencing, command
deadlines, and gravity float remain authoritative.

The Provider accepts new Contact sessions only while Manager has placed it in
`HOT`. `WARM` keeps fresh measured joint publication valid but reports the
motion capability not ready, and it owns no Basic control lease. Manager
authority is currently shadow-observed: signed upstream lineage,
the live Manager record, and the separate Basic fencing namespace are
published without treating advisory generation equality as authorization.

Low-torque or low-stiffness testing is prohibited for this load-bearing arm.
Locked joints use the full Basic-authorized POS_TOR torque ceiling in N·m after
their position is captured from fresh feedback. Non-locked joints reserve absolute gravity
effort plus the larger of their mapped wrench contribution and 20 percent of
configured maximum effort.

General collision planning is intentionally absent. A task-specific Skill is
responsible for establishing that its workpiece, acting point, and contact
path are appropriate. This absence does not disable joint limits, assembly and
frame checks, finite-number validation, feedback freshness, motion inhibit,
watchdogs, or lease fencing.

Every accepted move replaces the active endpoint or in-progress Cartesian
segment immediately. Internal segment IK knots are not a queue of Skill moves.
The Provider does not require that an endpoint be reached. Explicit relaxation,
inactivity timeout, authorization expiry, control fault, lease loss, motion
inhibit, and shutdown all fence the session and request Basic gravity float.
