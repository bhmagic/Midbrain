# Register Tool to Control Frame Boundary

`register-tool-to-control-frame` is a finite Skill, not a continuously resident
Provider. It composes three observations:

- a VLM-selected set of image landmarks and per-landmark confidence;
- `spatial-registration-rgbd` metric points with calibration, timestamp, and
  camera-route provenance;
- the robot's timestamped tool transform.

Reflective surfaces are handled explicitly. A landmark can request
`CLOSEST_TO_CAMERA`, which selects the closest valid sample inside the bounded
depth neighborhood while retaining the requested pixel, selected pixel, patch
statistics, and route provenance. This behavior is useful near a shiny blade or
handle, but it is not treated as proof that the selected depth belongs to the
tool.

The initial result is always review-only. It cannot command motion, publish a
transform, or activate a controller control frame. A rejected observation
cannot be authorized. An eligible candidate still requires a separate,
decision-specific authorization and the controller's physical safety checks
before a future publication path may be added.

The first geometry adapter uses three roles: an axis start, an axis end, and a
plane landmark. It supports an acting-point offset along the axis and produces a
full frame relative to the current robot tool frame. Additional adapters for
non-blade tools can be added without changing RGB-D registration or VLM routing.

This specialized three-landmark frame builder is not the general
effector-front locator. General distal front evidence, including paired
bare-gripper fronts and reflective-tip fallback to the same rigid tool body, is
owned by `locate-effector-front`.
