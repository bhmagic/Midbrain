# Translate Fabric Direction to World

This finite read-only Skill converts one explicit direction into the active
Fabric world frame. It is the shared coordinate boundary for tandem workflows
whose downstream Skill accepts only world-axis vectors.

The Agent supplies a non-zero `direction`, its `source_reference`, and any
available source frame, timestamp, or epoch identity. `ARM_BASE` binds the
configured arm base. `CONTROLLED_EFFECTOR_FRAME` binds the current configured
hand or tool control frame. `ACTIVE_WORLD` validates and preserves an already
world-framed direction.

The Skill normalizes the input, applies rotation only, normalizes the result,
and returns `direction_world`. Translation is never applied to a direction.
The result also carries the active world-frame ID, session epoch, calibration
revision, coordinate timestamp, and exact transform path.

For mixed-frame slicing, call this Skill for each non-world direction and copy
`direction_world` unchanged into the matching semantic field, such as
`slicing_direction_world`. Do not swap blade and slicing roles merely because
both values have the same coordinate type.

This Skill performs no movement, contact, lifecycle mutation, calibration, or
authorization. The downstream Skill remains responsible for validating its
own physical operation against current authority.
