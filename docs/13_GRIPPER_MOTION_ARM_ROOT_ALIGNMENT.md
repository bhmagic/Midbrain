# Gripper-Motion Arm-Root Alignment Plan

## Decision and scope

The next alignment path will estimate the stationary world-to-arm-base
relationship from controller-known gripper positions and RGB-D-observed
gripper positions. It is intended to become the ordinary, reusable alignment
path for a rigidly mounted camera and arm. It does not remove FoundationPose.

FoundationPose remains a deliberately slow initializer and diagnostic route.
The regular Agent may summon it only when the operator supplies this exact
request:

`Use FoundationPose to establish the stationary world-to-arm-base transform.`

Generic requests such as “establish both axes” must not silently start
FoundationPose. They will eventually select this movement-based workflow. Until
that implementation is complete, the Agent must report the missing workflow
instead of falling back to FoundationPose.

## Accepted functional evidence

The 2026-08-05 physical test established that the existing observation and
motion plumbing can collect a useful correspondence without a major functional
failure. The commanded motion was 0.25 m in world +Z with measured orientation
preserved. The RGB-D observation measured displacement
`[-0.0149, 0.0007, 0.2584] m`, a 0.2588 m distance, 0.9983 direction cosine
with world +Z, and 14.9 mm lateral error. Controller arrival and visual motion
were both confirmed.

That test produced two endpoint correspondences. It correctly did not activate
a six-degree-of-freedom root correction because one displacement vector cannot
observe rotation about that vector. This is evidence gathering, not yet full
alignment.

## Minimum geometry

Let `q_i` be the controller/FK position of a stable gripper landmark in the arm
base frame and `p_i` be the synchronized RGB-D position of the same landmark in
the stationary world frame. Estimate the rigid transform as
`p_i = R q_i + t`.

Three non-collinear gripper positions provide two independent displacement
vectors. Their cross product supplies the third basis direction, allowing
rotation and translation to be estimated. More observations improve noise
rejection and expose bad landmark or transform associations. A single
stationary position can refine translation only when rotation is already
trusted; it cannot solve a new six-degree-of-freedom transform.

The solver should use weighted rigid registration, initially Kabsch/SVD, with
weights derived from registered-depth support, gripper landmark confidence,
controller arrival residual, and temporal synchronization. It must reject
rank-deficient geometry, duplicate positions, mixed camera/VIO epochs, and
correspondences whose residuals are inconsistent with their uncertainty.

## Initial movement sequence

The first autonomous sequence should be bounded and easy to inspect:

1. Reach controller safe home and confirm at least 1x Kp plus gravity feed
   forward throughout motion and holds.
2. Ask the VLM to identify the operator-described calibration volume and the
   table. Refine those masks with SAM2, remove the arm mask plus its configured
   dilation, and use registered RGB-D geometry to verify a clear movement
   corridor. VLM language alone is not collision geometry.
3. Capture the safe-home gripper landmark as correspondence `P0`.
4. Command an approximately 0.20 m arm-base +Z displacement while preserving
   measured orientation. Using an arm-base direction avoids depending on the
   unknown world-from-base rotation to generate the calibration motion.
5. Confirm controller arrival, capture synchronized RGB-D, and record `P1`.
6. From the elevated pose, command an approximately 0.20 m non-collinear
   arm-base +X displacement if the verified corridor, IK, table clearance, and
   camera visibility permit it. Capture `P2`.
7. Solve the candidate transform from `P0`, `P1`, and `P2`; then validate it
   against the individual endpoints and the observed displacement directions.
8. Return to safe home and fully terminate physical control after a test run.

The actual displacement may be reduced to 0.15 m when the arm is mechanically
less stable, near an IK boundary, poorly visible, or near the verified free
volume. Larger well-conditioned displacements are preferred over many tiny
moves because fixed localization error occupies a smaller fraction of the
baseline.

## Optional refinement sequence

After the minimum transform is usable as a candidate, collect an adaptive star
around the elevated center rather than blindly demanding every point. Candidate
offsets are ±X, ±Y, and ±Z at 0.15–0.20 m. Each leg is accepted only when the
controller preview, semantic scene, camera visibility, and table clearance are
valid. Returning through the center between selected legs reduces long,
poorly-conditioned routing and supplies repeat observations for drift and
repeatability checks.

Use all accepted points in a robust fit, but retain at least one observation as
a held-out validation point. Report RMS and maximum translation residual,
angular consistency of observed versus commanded displacement vectors,
condition/rank, repeatability at the center, and each rejected observation.
More samples must not hide a systematic axis swap, sign error, timestamp
mismatch, or unstable visual landmark.

## Landmark and synchronization contract

The gripper landmark must have one explicit semantic definition shared by the
camera observation and FK conversion, such as the calibrated foremost-beak
mean. A FoundationPose gripper model origin and a VLM/SAM2 beak point are not
interchangeable without calibrated tool geometry.

Each correspondence must bind:

- camera Provider, instance, boot, calibration revision, frame number, and
  capture timestamp;
- VIO Provider identity, world frame, session epoch, tracking state, and pose
  timestamp;
- arm Provider identity, controlled-frame definition, joint/FK sample, arrival
  state, and sample timestamp;
- gripper observation method, mask/evidence reference, depth support,
  uncertainty, and material/depth status;
- the exact motion preview and execution outcome that produced the endpoint.

The workflow should consume coherent Fabric-hosted observations. Skills must
not poll camera, SAM2, VIO, or controller control APIs to assemble their own
private “latest” state. A small allowed delay is normal; lineage and
uncertainty matter more than wall-clock age alone.

## Fabric-hosted alignment state

The current before/after accumulator is useful evidence but remains
process-local and candidate-only. Replace it with a versioned alignment
Provider or Manager/Fabric-owned service that publishes:

- immutable correspondence records;
- the current observation set and geometric condition;
- candidate `world_from_arm_base` transforms with covariance/error bounds;
- validation and held-out residuals;
- activation, supersession, and rollback lineage;
- a translation-only close-range refinement candidate when rotation is locked.

The service may remain HOT when it gains an advantage from continuously
receiving already-published gripper observations and motion outcomes. HOT must
not mean that it starts camera inference, moves the arm, or silently activates
every estimate. The Agent or an upstream alignment Skill owns the finite
calibration sequence; Manager owns final activation and invalidation.

An accepted transform should have no arbitrary five-minute expiry. It remains
valid while mounted camera identity/calibration, VIO epoch and convention,
tracking evidence, arm identity/control-frame definition, and residual checks
remain consistent. A transform change must be published as a new version with
rollback evidence, never by mutating an active record.

## Close-range stationary refinement

Before a task intentionally moves the gripper close enough that its collision
spheres may meet a workpiece or obstacle, the upstream Agent may explicitly
request a stationary visual realignment. This is case-by-case, not an automatic
action after every motion.

With a trusted rotation, one synchronized FK/RGB-D gripper correspondence can
propose a translation correction. Recompile the semantic scene against that
candidate and retry planning before deciding that geometry is truly in
collision. The correction must remain a reviewed, bounded candidate until its
residual and lineage checks pass; merely collecting the point must not be
reported as having aligned the base.

## Workpiece and obstacle geometry near contact

General point clouds remain the preferred representation at distance because
they are fast, general, and do not require a brittle object model. Existing
20 mm gripper-ROI and 60 mm arm-base-ROI spheres are coarse environment
geometry, not contact geometry.

Semantic authority remains with the user or upstream task. Do not hard-code the
black mat, table, or every visible object as an obstacle. A VLM/SAM2 workflow
may locate and refine only the objects described by that authority; it must not
invent a broader blocking policy.

When the selected workpiece is very close, refine only that workpiece into a
denser, possibly overlapping sphere set derived from its point cloud or
surface. Preserve its object identity, uncertainty, and `WORKPIECE` role.
Density should increase as distance falls; it should not inflate the entire
scene or turn all visible geometry into obstacles.

For obstacle masks, apply a small inward erosion comparable to the arm-mask
dilation before generating spheres, then use depth and support-surface
consistency to reject spillover onto a nearby workpiece. Do not use the same
erosion blindly on a small workpiece, where it could erase the intended contact
surface.

If an updated obstacle scene still blocks the direct route, plan a valid
multisegment route or move to a clearly reported closest safe observation
pose. Workpiece contact requires an upstream approval bound to the named
workpiece, contact policy, and exact move. Without that approval, stop at the
closest allowed no-contact pose and request it. Ordinary POS_SPEED motion is
not itself a contact controller.

## FoundationPose boundary

FoundationPose remains available for difficult initialization, comparison,
and diagnosis. The exact phrase above is the only normal Agent-facing summon.
There is no automatic fallback from movement alignment, missing gripper depth,
poor registration geometry, or a generic axis request. Its finite lifecycle,
candidate review, activation checks, and separate compatibility Provider remain
unchanged after explicit invocation.

## Required negative tests

Before repeated hardware fitting, synthetic tests must inject each common
alignment failure and require either a specific rejection or an obviously large
visualized residual:

- RGB optical X/Y convention confused with world or arm-base axes;
- `world_from_base` confused with `base_from_world`, including multiplication
  order;
- quaternion component ordering or active/passive rotation confusion;
- timestamp mismatch across RGB-D, VIO, FK, and controller arrival;
- camera and FK observations bound to visually similar but physically
  different gripper landmarks;
- collinear, nearly collinear, or nearly duplicated calibration positions;
- a VIO epoch, camera boot/calibration, arm boot, or control-frame change inside
  one observation set;
- many noisy samples that conceal a systematic axis swap or sign error;
- arm occlusion of the table mask or obstacle spillover onto the workpiece;
- activation of a new transform without retained rollback evidence.

Passing a low aggregate RMS value is insufficient when one of these systematic
failures is present.

## Implementation order for the next iteration

Begin with the versioned Fabric contracts and a pure solver. Feed the accepted
two-point record plus synthetic third points into it before connecting new
physical motion. Only after the fit artifact clearly shows the expected axes
should a candidate be offered to Manager for motion-usable activation.

1. Define the Fabric schemas for gripper correspondences, observation sets,
   fit candidates, and activation lineage.
2. Move the existing process-local accumulator behind that contract and persist
   evidence across Agent turns and process restarts.
3. Implement the rank/conditioning checks and robust rigid fit with synthetic
   tests for swaps, signs, non-collinearity, noise, outliers, and timestamps.
4. Build the safe-home, +Z, and orthogonal-motion finite Skill with preview and
   scene checks at every leg.
5. Add a visual review artifact showing commanded/FK points, observed RGB-D
   points, residual vectors, fitted frames, and rejected samples.
6. Add Manager candidate activation, supersession, invalidation, and rollback.
7. Connect the optional stationary translation refinement to the no-contact
   approach planner.
8. Add adaptive close-range workpiece sphere refinement and obstacle-boundary
   spillover tests.
9. Compare latency, repeatability, held-out residual, and operational recovery
   against explicitly invoked FoundationPose before making movement alignment
   the ordinary generic-axis route.

## Acceptance target

The first implementation is complete when a fresh stationary rig can produce a
versioned, motion-usable transform from three or more non-collinear gripper
positions; repeat a held-out point within its reported error bound; survive
normal Agent/Fabric delay; reject mismatched epochs and bad geometry; visualize
the fit; return home and terminate safely; and perform all of this without
implicitly starting FoundationPose.
