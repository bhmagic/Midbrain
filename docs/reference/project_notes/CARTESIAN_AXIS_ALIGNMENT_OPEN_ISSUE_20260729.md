# Cartesian Axis Interpretation and Alignment Open Issue

Date: 2026-07-29
Status: open; physical motion remains bounded by controller preview and
operator authorization

## Statement

Cartesian-axis understanding and frame alignment remain a material challenge.
The stationary workcell transform is accurate enough for the completed
no-contact observation test, but the system does not yet have one
unambiguous, general rule that converts human direction words such as "up",
"front", "left", or "toward the object" into a controller-frame vector.

This is not only a naming problem. The same physical direction has different
coordinate components in:

- the camera-origin stationary world frame;
- the robot arm-base frame;
- the controlled-effector frame;
- an attached or held tool frame; and
- an object or task frame.

In the validated workcell, physical vertical was represented primarily by
positive world `Y` and positive arm-base `X`. That relationship came from the
active calibration transform; it must not become a hard-coded convention for
another camera pose, robot installation, or calibration revision.

## Evidence from the final shutdown lift

The operator requested a 20 cm upward lift before safe-home and shutdown.
The command was interpreted as a displacement opposite gravity and resolved
into arm-base coordinates using the current workcell understanding.

- The exact 20 cm controlled-frame target exceeded the configured positive
  arm-base workspace boundary by about 3 mm.
- A 19 cm preview was rejected for singularity proximity, IK residual,
  waypoint joint jump, endpoint joint travel, and aggregate joint travel.
- A 15 cm preview was rejected by endpoint joint travel.
- A 14 cm preview was rejected by joint-3 endpoint travel.
- A 13 cm preview passed collision, singularity, IK, joint-jump, endpoint
  travel, and aggregate-travel gates.
- The measured lift was approximately 12.67 cm.
- The one-shot controller reached its eight-second deadline with about 7 mm
  Cartesian residual, classified the result as
  `DEADLINE_FLOAT_BEFORE_ARRIVAL`, and confirmed gravity-float.
- The subsequent authoritative safe-home succeeded, and all workspace
  services stopped.

The result was safe, but it demonstrates why a user phrase and a raw
Cartesian axis must not be treated as interchangeable.

## Required architecture

A future directional command should carry explicit semantics, not only an
axis index:

- semantic direction, such as `OPPOSITE_GRAVITY`, `CAMERA_FORWARD`,
  `TOOL_FORWARD`, `OBJECT_NORMAL`, or `TOWARD_TARGET`;
- source frame and target controller frame;
- transform revision and timestamp;
- gravity vector and gravity-source provenance when vertical is intended;
- uncertainty and maximum allowed angular disagreement;
- requested displacement and controller-clamped displacement;
- the exact transformed vector used for planning; and
- operator-visible wording that states both the semantic direction and the
  resolved controller-frame vector.

Integrated Controller must remain responsible for workspace limits,
singularity avoidance, IK continuity, joint travel, collision clearance,
speed, and arrival classification after the semantic vector is resolved.

## Validation still required

- Verify the sign and scale of all three translated axes with small,
  separately authorized motions.
- Verify rotations about all three axes from at least two nonsymmetric
  effector orientations.
- Compare camera-observed displacement, robot forward kinematics, gravity, and
  the active world-to-base transform.
- Repeat after camera movement, VIO epoch change, robot-base movement, and
  calibration revision.
- Reject ambiguous symmetric pose estimates unless an independent kinematic or
  geometric observation resolves the ambiguity.
- Add replay cases for swapped axes, inverted axes, stale transforms,
  inconsistent gravity, and incorrect frame labels.
- Keep VLM direction descriptions as evidence, not final geometric authority.

Until these checks pass, natural-language Cartesian directions remain a
reviewed, bounded development feature rather than a general autonomous motion
interface.
