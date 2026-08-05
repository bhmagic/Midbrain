# Midbrain Integration

FoundationPose has two explicit Midbrain routes:

1. `foundation_pose_object_localization` is a finite Skill used inside a
   bounded parent operation. It owns estimator sessions and releases GPU
   resources before returning.
2. The FoundationPose Provider preserves the former session/tracking interface
   for compatibility diagnostics and guarded route comparison.

The Provider is not the ordinary generic alignment path, is not an automatic
fallback, and should not remain `HOT` without a task-specific reason.

Provider ID: `perception.object_pose.foundation_pose`

The Manager capability catalog and `manifest.json` are authoritative for the
current callable surface. The compatibility request actions are:

- `estimate` for one initialized result;
- `track` to initialize and retain a session;
- `relocalize` after a tracked session loses the target;
- `stop` to end an owned session;
- `release_resources` only after every session has stopped;
- `status`, `list_models`, and `reload_models` for diagnostics and registry
  maintenance.

Initialization evidence may be a reviewed local `mask_path`, a validated
`bounding_box`, or a current `perception.object.mask` observation. Bounding-box
coordinates and accepted request fields belong to the checked-in request
schema and implementation; callers should validate against those sources
rather than copying an example payload from a changelog.

## Responsibility boundary

FoundationPose estimates the camera-relative pose of a known rigid model from
reviewed RGB-D initialization evidence. It does not define world space, grant
motion authority, decide that a transform is safe to activate, or replace the
parent alignment workflow.

The parent finite Skill owns:

- motion inhibit and stationary-workcell checks;
- synchronized RGB-D and VIO-epoch evidence;
- target identity, reviewed regions, and masks;
- sampling, outlier rejection, symmetry handling, and uncertainty;
- conversion into a reviewed world/arm alignment candidate; and
- verification that every owned estimator/session resource was released.

Manager owns final activation, revocation, and supersession of a motion-usable
workcell transform.

## Observations and frames

FoundationPose consumes camera observations only when they explicitly declare
the native optical convention. Pose outputs are camera-relative measurements
with source capture time, Provider/boot identity, model and session identity,
calibration revision, quality, and validity.

Native optical point components use `camera_system_x`, `camera_system_y`, and
`camera_system_z`. A camera-relative model pose must not be published or
interpreted as world-frame authority.

The default Base reporter's semantic frame `robot/arm_root` and the Integrated
controller frame `rebot_arm_base` are not aliases. The former is a raw visual
model frame; a reviewed alignment candidate and Manager activation are
required before any relationship can become motion-usable. Likewise, the
default Gripper reporter is a centered rigid mesh frame, not a TCP, controlled
frame, or task action point.

## Large payloads

RGB and depth bytes remain behind validated BufferRefs. A consumer must reject
a recycled generation, obsolete camera boot, mismatched calibration, invalid
alignment, or evidence from a different VIO epoch. Copying a payload for a
bounded estimator attempt does not extend the authority of its metadata.

## Lifecycle

The finite route loads and releases estimator resources inside one parent
operation. The compatibility Provider follows Manager `COLD`, `WARM`, and
`HOT` lifecycle and must reject resource release while foreign sessions remain.
After a compatibility run, close every owned session and request resource
release or transition the Provider to `WARM`.

The normal Agent may invoke FoundationPose only for the exact documented
operator request in [Setup and Operation](../../../docs/03_SETUP_AND_OPERATION.md).
Generic alignment language must not silently start it.

## Related documentation

- [Compatibility Provider README](../README.md)
- [Finite FoundationPose Skill](../../../skills/foundation_pose_object_localization/README.md)
- [Stationary Alignment](../../../skills/stationary_world_arm_alignment/README.md)
- [Active movement-alignment plan](../../../docs/13_GRIPPER_MOTION_ARM_ROOT_ALIGNMENT.md)
