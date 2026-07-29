# Evaluation and design

## Feasibility

The alignment is practical as a finite Skill, with four explicit limits:

- FoundationPose registration is a long, monolithic GPU operation. Its independent control-server health endpoint exposes phase, liveness, last frame, result count, error, and latency, but no real percentage. The monitor therefore reports indeterminate progress with live evidence.
- The base's symmetry is exactly a two-candidate choice. The Skill never searches intermediate yaw angles.
- A VLM pixel alone is not metric. Even VLM-only refinement still reads aligned depth, the current VIO transform, and arm kinematics; "VLM-only" means FoundationPose is not started.
- The VLM gripper point, robot tool frame, and physical beak are not identical. The base run uses a documented weighted rough fusion and learns a tool-local beak offset for later refinement. A measured offset in configuration is preferable.

## FoundationPose-base + VLM-gripper data flow

1. Start or retain camera, VIO, and arm pose inputs, then require VIO `TRACKING`.
2. Verify the tool transform is stationary over the configured window.
3. Acquire motion inhibit.
4. Read calibration and VIO state, then fetch and immediately copy one exact aligned RGB-D bundle. If its shared-memory slot was recycled, wait for a fresh Fabric bundle and retry the copy.
5. Ask `gpt-5.6-luna` for tight base/gripper boxes, mask seed points, jaw state, and one or two foremost beak points.
6. Generate local binary masks and submit one FoundationPose session for the base only.
7. Poll FoundationPose's existing `/health` endpoint while collecting timestamped Fabric poses.
8. Transform every camera-relative FoundationPose sample into the same VIO world at its source timestamp.
9. Compare the base semantic +Z axis to VIO world-up (+Y). Correct an upside-down FoundationPose hypothesis with a semantic 180-degree X rotation before projection; the later 0/180 yaw decision remains independent.
10. Project the base mesh's 3D box and semantic XYZ axes onto the live RGB using the upright averaged camera-relative pose.
11. Send that exact RGB composite and the eight-angle base CAD atlas to the VLM. If explicitly rejected, reset every attempt session and create new session IDs so FoundationPose performs a fresh register-from-mask operation. Only one retry is permitted.
12. Robustly average accepted base samples, test only the 0- and 180-degree yaw candidates, and select the one whose arm tool is closest to the gripper evidence.
13. Fuse the configured translation estimates, define a new world frame with VIO orientation and the reference camera optical center as origin, then publish the transforms and overlay artifact.

The `foundation_base_gripper` mode runs FoundationPose on both objects. It is exposed as the slow dim-scene mode. It uses the FoundationPose gripper and arm kinematics to resolve base yaw, gives VLM translation only a small auxiliary weight, and applies the same base overlay validation and bounded fresh-registration retry.

## Beak cases

- Closed gripper: one foremost beak point.
- Open gripper: one point per beak, then mean their two 3D locations.
- Clear, camera-facing, empty gripper: the local near-depth cluster may replace the center-pixel depth when it is within the configured 5 cm span.
- Holding an object, not facing the camera, or uncertain geometry: local-minimum selection is disabled and the VLM pixel's aligned depth is used. Missing valid depth fails closed.

## VLM-gripper-only adjustment

The public mode is `vlm_gripper_only`. It requires a valid prior alignment, the same VIO epoch by default, valid depth, and learned or configured tool-to-beak geometry. It locks rotation and the previous symmetry decision. A first correction over 5 cm triggers two additional VLM inferences on the same stationary RGB-D frame; the closest pair of the three translation estimates is averaged and the third is discarded. The former 5 cm hard rejection is removed.

## Upstream measurement labels

Result schema version 3 makes the three concrete modes discoverable through
the manifest, repeats the selected source contract in every result, and embeds
an expiring calibration candidate that is not motion-usable before review.
VLM-derived gripper evidence is labeled `VLM_RGBD_BEAK` with semantic point
`FOREMOST_BEAK_MEAN`. FoundationPose-derived evidence is labeled
`FOUNDATIONPOSE_GRIPPER_POSE` with semantic point `GRIPPER_MODEL_ORIGIN`.

The dual-FoundationPose result carries both records because its VLM beak remains a low-weight translation input. The result explicitly marks the two positions as not directly comparable: an upstream adjustment must transform the model origin to the physical beak using calibrated tool geometry first.

## Safety and quality

The Skill accepts an armed arm or a home arm, but both must be stationary. It does not command motion. A moving arm fails preflight unless the caller explicitly enables active-control interruption. No partial transform is published on failure or cancellation. Results retain VIO epoch, camera calibration revision, source timestamp, fusion diagnostics, symmetry scores, and VLM evidence.

## Expiry and renewal

The isolated Skill includes a compatibility keeper. With a future Manager lease ID it renews periodically and fails closed after repeated renewal failures. With the current Manager it detects the legacy non-expiring inhibit and always releases it in cleanup.

The Manager-side TTL/renewal change is a medium-small core project: approximately one focused implementation day plus one validation day. It requires a lease ID, monotonic expiry, a renew endpoint, compare-and-release behavior, background pruning, restart semantics, Fabric publication fields, arm-provider compatibility checks, and race/expiry tests. It should be delivered separately because it changes a safety contract shared by every motion provider.
