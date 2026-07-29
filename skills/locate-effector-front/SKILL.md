---
name: locate-effector-front
description: Locate the most distal visible point of a robot effector, mounted tool, or held tool that has reliable registered-depth evidence. Use for general effector alignment when one front point, or the two front points of a bare two-jaw gripper, must be registered in 3D without assigning a task-specific drill, hammer, blade, or other action point.
---

# Locate Effector Front

Use one synchronized RGB and registered-depth observation.

1. Treat "front" as most distal along the visible rigid assembly away from the
   wrist or arm. Do not interpret it as closest to the camera, lowest in the
   image, or the task-specific action point.
2. Inspect RGB, registered depth, and their validity overlay on the registered
   depth grid.
3. Select the most distal pixel that belongs to the effector or attached/held
   tool and has valid depth. For a reflective or thin tool with missing distal
   depth, retreat only as far as needed along the same rigid assembly. This may
   select the tool body or handle.
4. For a bare two-jaw gripper with two distinct fronts, return both points.
   Downstream control math uses the mean of the two registered 3D points, not
   the mean pixel.
5. Reject the observation when the rigid assembly is ambiguous, the selected
   pixel belongs to the background or held work object, or both required
   gripper fronts cannot be located reliably.
6. Preserve the source timestamp. Revalidate camera binding after VLM
   inference and apply this Skill's completion-age limit.

Return a read-only reference. Do not publish a control frame, infer hidden
geometry, authorize motion, or substitute a specialized action point.

Use a separate narrowly prompted Skill for drill tips, hammer faces, cutting
edges, dispensing nozzles, or other task-specific action geometry.

## Change log

- 0.1.0 (2026-07-29): Added the general registered-depth effector-front
  landmark contract, including the paired-front rule for a bare two-jaw
  gripper, nearest distal valid-depth behavior for reflective or thin tools,
  preserved source timestamps, post-inference freshness checks, and a
  read-only result that cannot publish or authorize a control frame.
