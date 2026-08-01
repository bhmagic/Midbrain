# Spatial Frame Convention and Semantic Direction Contract

Status: v0.4 working draft.

Convention identifier:
`MIDBRAIN_X_FORWARD_Y_LEFT_Z_UP_V2`.

## Canonical three-dimensional language

Unqualified spatial language is always resolved in the current workcell/world
frame:

- `FRONT` and `FORWARD`: positive X.
- `BACK` and `BACKWARD`: negative X.
- `LEFT`: positive Y.
- `RIGHT`: negative Y.
- `UP`: positive Z and opposite measured gravity.
- `DOWN`: negative Z and parallel to measured gravity.

The same axis order applies to `local_vio/<epoch>`, `workcell`, `robot_body`,
and a normally mounted robot base. A robot base may have an arbitrary physical
orientation in the world. That orientation is represented by a transform and
does not redefine any semantic direction.

Plain `up`, `front`, `left`, or `right` must never select a camera optical axis,
an arm-base axis, a joint axis, or an image direction.

`NORTH`, `SOUTH`, `EAST`, and `WEST` are not aliases for robot front/back or
world X/Y. They require a separately calibrated geographic or surveyed map
frame. Without that authority, the agent asks for `front/back/left/right` or an
explicit framed vector and does not submit motion.

## Camera frames

Raw camera geometry uses the conventional optical frame and retains the native
sensor axes:

- `camera_optical/<camera_id>` positive X: image right.
- `camera_optical/<camera_id>` positive Y: image down.
- `camera_optical/<camera_id>` positive Z: optical forward.

Existing hardware-specific frame identifiers such as
`femto_bolt_color_optical_frame` are aliases in the
`CAMERA_OPTICAL_X_RIGHT_Y_DOWN_Z_FORWARD_V1` convention. Optical coordinates
may cross Provider boundaries for calibrated projection, depth deprojection,
VIO, registration, and point-cloud processing. They must never cross a boundary
as anonymous `xyz`: every payload includes a frame identifier, convention
identifier, units, timestamp, and calibration revision.

At component boundaries, native optical components are named
`camera_system_x`, `camera_system_y`, and `camera_system_z`. A metric point is
serialized as `camera_system_point_m` with those three named members. Internal
linear-algebra arrays may use `camera_system_xyz_m`, but generic names such as
`camera_xyz`, `camera_point`, or an unlabeled `[x, y, z]` are prohibited for
optical data.

`camera_level/<camera_id>/<epoch>` is a derived three-dimensional frame at the
camera optical center. Its positive X is optical forward projected onto the
gravity-horizontal plane, positive Y is left, and positive Z is opposite
gravity. It ignores camera roll and pitch by construction. When optical forward
is too close to gravity vertical, leveled camera front is undefined and the
frame must not be published.

Two-dimensional image language is a separate, explicit vocabulary:
`IMAGE_LEFT`, `IMAGE_RIGHT`, `IMAGE_UP`, and `IMAGE_DOWN`. It is used only when
the operator explicitly requests image, pixel, screen, or other 2D reasoning.

## Local VIO frame

Every VIO reset creates a new `local_vio/<epoch>` using this convention:

- positive Z is opposite the calibrated gravity observation;
- positive X is the initial camera/body forward direction projected onto the
  gravity-horizontal plane;
- positive Y completes the right-handed basis and points left.

The gravity vector in this frame is `[0, 0, -g]`. Migration from the former
Y-up estimator always creates a new epoch. Old trajectories, maps, cached
points, calibration candidates, and previews are historical
`LEGACY_Y_UP_V1` data and must not be reinterpreted.

Accelerometer bias, scale, axis metadata, hardware identity, device
calibration revision, and camera/IMU extrinsics are unchanged by this world
basis migration.

## Robot and arm frames

`robot_body` and a conventionally mounted `arm_base` use positive X forward,
positive Y left, and positive Z up. Kinematics continue to use the arm model's
local base frame. A world semantic vector is converted to arm-base coordinates
with the current timestamped rotation:

`direction_arm = transpose(rotation_world_from_arm) * direction_world`

A current reviewed, motion-usable transform always has priority, even if a
caller also supplies a prior upright-mount attestation. The attestation must
not replace or bypass measured frame authority.

If no reviewed world-to-arm transform exists, the system fails closed. A
bounded development fallback may be created only after the operator explicitly
attests that arm-base positive Z is opposite gravity and arm-base positive X is
aligned with workcell forward. This arm-mount assumption does not depend on
VIO, a camera frame, or a workcell alignment. It is tied to controller
provider/instance/boot/configuration identity and one exact preview. It is an
authorization of an assumption, not a calibration.

The arm-mount fallback applies only to ordinary semantic
`up/down/front/back/left/right`. An explicit signed world-axis request
(`world +X/-X/+Y/-Y/+Z/-Z`) always requires a reviewed, motion-usable
world-to-arm transform. It must never be reinterpreted as the corresponding
arm-base axis or trigger an upright-mount question. Raw arm-axis intent remains
available only through the explicit `ARM_BASE_POSITIVE_*` and
`ARM_BASE_NEGATIVE_*` vocabulary.

Camera verification is a separate evidence layer. Before it may be used, the
operator confirms that the camera and IMU are rigidly fixed together and the
rig can remain stationary. A non-destructive check may then start or verify the
camera and VIO Providers, but must not reset the VIO epoch or revoke workcell
calibration. A fixed-rig check uses a VIO-local, time-bounded stationary
attestation; it must not acquire the whole-robot motion inhibit or revoke an
arm-controller lease. Before and after effector observations use the same
convention-V2 world frame and session epoch. They may confirm or contradict
controller completion, but they never redefine the arm command direction.

Raw arm-axis requests use explicit names such as `ARM_BASE_POSITIVE_X`; they
are never inferred from ordinary language.

## Framed spatial payload

Any cross-component point, vector, pose, or transform includes:

- `frame_id`;
- `convention_id`;
- `observed_at_us`;
- `session_epoch` when the frame is resettable;
- `calibration_revision` when calibrated hardware contributed;
- units in the field name or an explicit `units`;
- transform-path provenance when converted from another frame.

Semantic motion additionally includes the original semantic direction,
resolved unit vector, source and target frame, transform timestamp, transform
path, convention revision, and any operator attestation.

Missing, stale, ambiguous, convention-mismatched, or review-pending frame
evidence is a rejection condition. The only non-transform fallback is the
explicit, preview-scoped upright arm-mount attestation defined above.
