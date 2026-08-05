# Next Iteration Handoff: Gripper-Based Arm-Root Alignment

Read [Gripper-Motion Arm-Root Alignment](../../13_GRIPPER_MOTION_ARM_ROOT_ALIGNMENT.md)
before changing the alignment path. The last 0.25 m physical motion test is an
accepted functional checkpoint with no major flaw. It proved controller motion,
before/after RGB-D observation, and the candidate accumulator; it did not yet
solve or activate the full arm-root transform.

## Decisions that should not regress

- FoundationPose is retained. The regular Agent may invoke it only with the
  exact sentence `Use FoundationPose to establish the stationary
  world-to-arm-base transform.` It is not a generic-axis default or automatic
  failure fallback.
- Ordinary generic axis alignment should become the finite gripper-motion
  sequence after that sequence is implemented.
- General point clouds are preferred while far from objects. Dense or
  overlapping spheres are reserved for the selected workpiece at very close
  range.
- User/upstream descriptions own semantic labels. Do not hard-code the black
  mat, table, or every visible object as an obstacle.
- Fabric supplies coherent best-available observations. Skills should not jam
  Providers by polling their control APIs directly.
- Delay is normal in an agentic workflow. Evaluate lineage, synchronized
  timestamps, uncertainty, and source cadence instead of using a tiny universal
  stale timeout.
- Base realignment is prompted and case-specific. It must not run after every
  movement or silently activate a candidate.
- Every physical test ends at safe home followed by full safe termination.

## Immediate engineering gaps

1. The current arm-root realignment accumulator is process-local. Its two-point
   result is evidence only and disappears with the owning process.
2. Three non-collinear point correspondences are required for a full rigid fit.
   A single before/after displacement cannot observe rotation about that
   displacement axis.
3. Camera and FK must refer to the same physical gripper landmark. A
   FoundationPose model origin and an RGB-D foremost-beak point require explicit
   calibrated tool geometry before comparison.
4. The finite safe-home, +Z, orthogonal move sequence does not yet exist as a
   discoverable Skill.
5. The resulting candidate still needs Fabric persistence, Manager activation,
   supersession, invalidation, and rollback.
6. Stationary translation-only realignment is not yet connected to the
   no-contact approach retry path.
7. Close-range workpiece refinement needs distance-adaptive dense spheres; the
   current 20 mm/60 mm environment spheres are too coarse for contact geometry.
8. Table-mask sphere spillover needs mild obstacle-mask erosion plus depth and
   support-plane consistency before sphere generation.

## Alignment traps to test explicitly

- RGB optical X/Y convention versus world and arm-base axes;
- `world_from_base` versus `base_from_world` multiplication order;
- quaternion component ordering and active/passive rotation confusion;
- timestamp mismatch between RGB-D, VIO, FK, and controller arrival;
- visually similar but physically different gripper landmarks;
- collinear or nearly duplicated calibration positions;
- VIO epoch, camera boot, camera calibration, arm boot, or control-frame changes
  within one observation set;
- fitting many noisy samples that conceal a systematic sign or axis error;
- arm occlusion of the table mask and obstacle spillover onto the workpiece;
- activating a new transform without retained rollback evidence.

Synthetic tests should inject each mistake and require a specific rejection or
large visible residual. Do not proceed directly to repeated hardware fitting
until those tests discriminate the failures.

## Suggested first work session

Start with the versioned Fabric contracts and a pure solver. Feed the accepted
two-point test record plus synthetic third points into it. Then connect the
existing before/after collector and build the finite calibration motion Skill.
Only after the fit artifact clearly shows the expected axes should the new
candidate be offered to Manager for motion-usable activation.

## Contact-planning reminder

When a close-range route appears to collide, the intended order is: explicitly
requested stationary translation refinement, semantic-scene recompile, route
retry, then either an alternate route/closest safe observation pose for an
obstacle or an object-bound contact approval for a workpiece. A `WORKPIECE`
label means contact is semantically allowed unless `NO_CONTACT` overrides it,
but it does not replace authorization or a contact-capable controller.

The present priority remains automation and outside-agent compatibility. New
contracts should be discoverable and versioned, expose their limitations, and
return actionable continuation data rather than requiring an upstream model to
reverse-engineer a private Provider failure.
