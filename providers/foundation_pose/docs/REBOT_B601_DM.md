# Default reBot B601-DM profile

## Base reporter

Model ID:

`robot_arm_root`

Role:

`robot_base`

Rigid geometry:

- `01_BASE_Plate.step`
- `01_BASE_Link.step`

Prepared mesh:

`Base_clean_centered.obj`

Semantic frame:

`robot/arm_root`

Observed frame:

`observed_object/rebot_b601_dm/base`

The Base centered mesh subtracts the exported Base bounding-box center. The
registry applies:

`mesh_from_semantic translation Z = -0.0446249945 m`

so the published visual measurement reports the configured arm-root frame
rather than the mesh centroid.

The physical geometry is close to 180-degree yaw symmetry. A reversed visual
hypothesis is acceptable for the Provider; consuming Skills may resolve it
using other information.

## Gripper reporter

Model ID:

`robot_gripper_slider_support`

Role:

`robot_gripper`

Rigid geometry:

`01_Rail_Bracket.step`

Prepared mesh:

`Gripper_clean_centered.obj`

Semantic/reporting frame:

`robot/gripper_slider_support_center`

Observed frame:

`observed_object/rebot_b601_dm/gripper_slider_support`

For the publication default, `mesh_from_semantic` is identity. The reported
frame is therefore the centered rigid Rail-Bracket mesh frame. It is
intentionally not labeled as TCP or URDF end-effector origin.

The central bracket is often partially occluded by other gripper structure.
Initialization masks should contain only visible Rail-Bracket pixels.

## Provenance

Upstream:

https://github.com/Seeed-Projects/reBot-DevArm

Prepared from upstream commit:

`0d74520357b46be02e07104c0d1bbb4e46789aef`

Hardware license:

CERN-OHL-W-2.0

See `defaults/rebot_b601_dm/UPSTREAM.md`,
`defaults/rebot_b601_dm/MODIFICATIONS.md`, and the retained STEP source.
