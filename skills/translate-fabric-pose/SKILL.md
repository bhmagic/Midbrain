# Translate Fabric Pose to World

This finite read-only Skill rigidly transforms a position and orientation into
the active Fabric world frame. Position is always supplied in metres and
orientation is always an XYZW quaternion. Both values use the same declared
source frame.

The Skill validates and normalizes the input quaternion, applies the complete
source-to-world rigid transform, and returns `target_position_world_m` plus
`target_orientation_world_xyzw`. The output includes the active world-frame
ID, session epoch, calibration revision, coordinate timestamp, and transform
path.

`ARM_BASE` and `ACTIVE_WORLD` use the current reviewed world authority.
`CONTROLLED_EFFECTOR_FRAME` additionally reads the timestamped current
controlled-frame transform from Fabric. When upstream frame, timestamp, or
epoch identity exists, the Agent copies those values unchanged into the
translator instead of reconstructing them.

The output is coordinate evidence, not a motion instruction. No current
Midbrain tool is implicitly upgraded into world-pose motion by this Skill. A
future consumer must explicitly accept the two output fields and independently
retain its own preview, authorization, collision, contact, and completion
boundaries.
