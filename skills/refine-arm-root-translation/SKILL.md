---
name: refine-arm-root-translation
description: Refine only the XYZ translation of an existing world-to-arm-base alignment from one or more independent observations of a profile-defined rigid effector landmark while preserving the active rotation exactly. The arm may move slowly during capture when timestamped FK bounds landmark motion within the profile limit. Adoption defaults to one; the Skill never moves the robot.
---

# Refine Arm-Root Translation

Use one synchronized RGB-D observation, timestamped arm FK, an active six-DoF
alignment, and one visual landmark from the active effector profile.

At the host boundary, verify the current profile-selected controlled-frame to
arm-base FK path before starting VLM work. If the local arm observations cannot
bracket a current timestamp, return a typed, non-mutating dependency result
with the existing Provider lifecycle tool and the preserved refinement
arguments. Permit one HOT recovery followed by one fresh retry. If the same
dependency remains, report a transform-publication fault and do not loop.
Keep this readiness and recovery policy in the Skill-specific host bridge; it
does not belong in VLM prompts or arm-alignment mathematics.

1. Keep the camera mounting and robot base rigid, but do not require an arm
   stationary-hold flag. Read the exact RGB and registered-depth hardware
   timestamps from one synchronized bundle. Use the registered-depth timestamp
   for metric camera pose and FK because the accepted 3D point comes from that
   channel.
2. Require Fabric to bracket every capture-window timestamp with transform
   history. Zero FK and camera-pose extrapolation is allowed: wait a bounded
   time for a later sample, then fail closed if interpolation cannot be formed.
3. Build a capture window that covers RGB/depth timestamp skew, the profile's
   camera timing margin, and any feedback age required by the profile's declared
   timestamp semantics. Sample profile-defined FK poses
   across that window. Transform the selected landmark's actual tool-frame
   coordinate through each pose and reject when the largest pairwise motion is
   over the configured limit, initially 5 mm. This automatically includes
   rotation-induced displacement for landmarks offset from the controlled
   origin. Treat joint-observation freshness as timing provenance, not an
   independent motion gate. When it is older than the profile's preferred age,
   use the profile's conservative maximum feedback latency and let exact
   transform bracketing plus measured landmark motion decide acceptance.
4. Treat the copied RGB-D bundle and its bracketed FK samples as immutable.
   Arm motion after the capture window, including while the VLM is running,
   does not invalidate it. Before updating state, revalidate camera binding,
   world tracking, arm/camera identity, and the active calibration revision.
   Never require the user prompt to claim that a stationary hold exists.
5. Require a trusted active rotation and a known tool-frame coordinate for the
   selected landmark. How the active alignment was initially established is
   outside this Skill's contract.
6. Require the active arm model ID, arm model revision, arm-base frame, and
   effector profile revision to match the selected profile before interpreting
   FK. Keep arm-specific frame names, terminal-joint geometry, feedback timing
   semantics, capture-motion limits, landmark bindings, and reference-asset IDs
   in the profile. A
   replacement effector or held tool needs a new profile revision with its own
   qualified terminal-joint geometry and visual landmarks.
7. Show the VLM both RGB and registered depth. Require separate RGB and depth
   coordinates for each named physical feature on the Skill-owned canonical
   0-to-1000 YX grid. Convert each coordinate deterministically to its own
   image resolution; never interpret a model-native normalized coordinate as
   a literal source pixel or snap missing depth to a coded neighbor. Render a
   separate registered-depth validity channel where white is usable and
   magenta is invalid. If a selected exact pixel is invalid, show the rejected
   markings to the VLM and allow one full-response reselection attempt. The VLM
   must select a valid pixel on the same semantic surface; never interpolate,
   infer, or silently snap depth. Reject after that single retry. State the
   complete response contract in the VLM prompt; a malformed response is a
   rejected observation that shows the exact input channels and never mutates
   active alignment state. Normalize VLM transport failures returned by the
   host into the same non-mutating evidence result. Let the host own RPC/VLM
   deadlines so a timed-out serialized request cannot desynchronize the Skill
   protocol.
8. Register each selected depth pixel independently in 3D. For paired
   landmarks, calculate the mean of the registered 3D points, not a mean image
   pixel.
9. Estimate only the three arm-root translation parameters. Copy the active
   rotation without modification.
10. Calculate the raw full translation delta before applying the caller's
   adoption factor. When the raw delta exceeds the configured threshold, send
   raw RGB, registered depth, depth validity, the full marked overlap, and a
   magnified marked-landmark crop to one additional VLM quality review. Tell
   the reviewer to reject a crosshair centered on empty space or outside the
   named rigid feature. Run this review for observation-only calls and calls
   that a delta limit will subsequently reject. The review must independently
   inspect the raw channels rather than trusting the first VLM's confidence.
11. The public multi-sample refinement mode accepts `sample_count` from one to
   five and defaults to one. Capture every sample independently and
   sequentially. Each sample must pass exact-depth resolution, capture-motion
   validation, and any required second VLM review. Compute the arithmetic mean
   of the N raw XYZ correction vectors, apply the caller's adoption factor to
   that mean once, and perform at most one atomic state update. Never update
   between samples and never create a parent-alignment chain. Report every raw
   vector plus component standard deviation and maximum distance from the
   mean; do not silently discard a statistical outlier.
12. Scale both the raw-delta and adopted-step limits by `sample_count`, so five
   samples receive five times the base bounds. Apply these scaled bounds only
   to the arithmetic mean after all samples pass their individual semantic
   reviews. Let the caller reduce its adoption factor and retry from fresh
   timestamp-coherent observations; never silently clamp a correction.
13. Treat the second VLM result only as PASS, FAIL, or UNRESOLVED. A failed or
   unresolved review rejects the invocation; it does not return replacement
   coordinates.
14. Return normalized Midbrain visual-evidence annotations for the exact image
   channels used by the VLM. Include old and proposed arm-base origins and old
   and proposed FK predictions for the selected visual landmark back-projected
   through the captured camera pose. These predictions are not necessarily the
   controller IK origin. Record whether one depth reselection was needed and its bounded
   outcome in result provenance. Preserve the true pixel coordinates and
   use labeled image-edge direction markers when a projectable point falls
   outside the image. SVG is rendered by the existing Agent UI.
15. Apply an accepted update only through an atomic active-revision match. Keep
   one compact current state and bounded diagnostics; do not build a recursive
   parent-candidate chain. Manager may retain a bounded append-only activation
   and rollback journal outside that active-state object. Preserve a Manager
   HTTP conflict across the external Skill process boundary and return the
   normal non-applied stale-state result; do not turn compare-and-swap rejection
   into an opaque Skill crash.

If omitted, `sample_count` is one and `adoption_factor` is 1.0. An adoption
factor of zero is observation-only and must not increment the active
calibration revision. This Skill creates no robot target, controller preview,
motion authorization, or physical command. The recommended user-facing name is
"multi-sample refinement," for example, "refine the arm alignment with VLM
using 5 samples." Treat "5x refinement" only as a friendly alias because it can
otherwise sound like multiplying one correction instead of averaging five
independent observations.

The first profile observes the mean of the two lateral endpoints of the rigid
neon-green Rail-Bracket on every default call. Keep the controller IK/FK origin
at the gripper-tip midpoint. Apply the user-measured vector from rail center to
controller tip as `[+0.080, 0, 0]` metres along controlled/final-joint +X. Store
the solver-facing controlled-origin-to-observed-landmark point as the exact
inverse `[-0.080, 0, 0]` metres. Rotate this offset with the timestamped
controlled-frame FK. Reconstruct the controller-tip position from the observed
rail center first, then solve the base translation while preserving base
rotation exactly. Never add 80 mm in world or arm-base X and never ask the VLM
to infer the tip. The profile also pins the current bare-gripper
terminal-joint translation and rotation to the existing arm-model revision.

The no-hardware-modification baseline is a profile-qualified rigid proximal
feature with reliable depth and an explicit bidirectional offset to the
controlled frame. An optional non-reflective ball landmark requires a
separately revised profile and is never a baseline dependency. Landmark
substitution is explicit because each landmark owns a different controlled-
frame coordinate.

Versioned machine-readable schemas for the effector profile, both VLM outputs,
the compact active state, and the refinement result are retained in `schemas`.

The timing margin is a conservative profile setting, not proof that every
camera timestamps the exposure midpoint. Remaining sources of error include
RGB rolling shutter or long exposure, unreported RGB/depth clock offset or
drift, changing encoder feedback latency, and nonlinear motion between FK
samples. Prefer provider metadata for exposure interval and clock uncertainty
when it becomes available; widen the capture window until then. Depth at a
reflective edge can also remain wrong even when temporal alignment passes, so
same-surface VLM review and exact-depth validation remain mandatory.

When diagnosing observed VIO body-pose freshness or arm-FK bracketing failures,
read `references/vio_and_arm_fk_timestamp_anomaly_handoff.md`. Keep those
provider timing paths separate and preserve fail-closed timestamp and identity
checks.

The manifest owns this Skill's host-adapter and setup entrypoints. Keep the
hardware/VLM bridge in `python/refine_arm_root_translation/host_adapter.py` and
the numerical RPC runtime in the Skill-private `.venv`; Test Agent may provide
only the generic host-service contract and discovery loader. Declare this Skill
as high latency because multi-sample requests perform sequential VLM calls.
