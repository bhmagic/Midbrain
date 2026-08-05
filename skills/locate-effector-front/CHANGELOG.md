# Changelog

## 0.2.1 - 2026-08-03

- Replace the three-panel VLM input with one RGB-on-depth-grid view whose
  invalid-depth pixels are visibly dimmed.
- Standardize VLM front points on normalized 0-1000 coordinates and convert
  them once in host code, while retaining version 1 native-pixel inputs.
- Register switchable RGB, registered-depth, and RGB-depth evidence channels
  with shared effector-front annotations in the Agent UI.

## 0.2.0 — 2026-07-30

- Named raw optical point components `camera_system_x`,
  `camera_system_y`, and `camera_system_z`.
- Added explicit source optical and target world convention identities to
  agent results.

## 0.1.0 — 2026-07-29

- Added the general registered-depth effector-front landmark contract.
- Added the paired-front rule for a bare two-jaw gripper, using the mean of the two registered 3D points for downstream control math.
- Added nearest-distal-valid-depth behavior for reflective, thin, or sharp tools whose visual tip does not have reliable depth.
- Preserved source timestamps and required post-inference camera-binding and completion-age checks.
- Kept the result read-only: it cannot publish a control frame, authorize motion, or substitute a task-specific drill, hammer, blade, nozzle, or other action point.
