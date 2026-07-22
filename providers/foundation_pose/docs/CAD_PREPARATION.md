# Preparing CAD for another robot

The CAD helper is optional and never runs as part of Provider startup.

## 1. Choose rigid targets

Do not use one mesh for a complete articulated robot.

Good targets include:

- a fixed base housing;
- a wrist bracket;
- a rigid gripper support;
- a camera mount;
- another mechanically rigid reference.

A robot may expose multiple targets simultaneously.

## 2. Preserve the original frame

Export the selected rigid target to OBJ while preserving the CAD coordinate
frame that will be used to define semantic transforms.

Do not arbitrarily move the geometry to the origin before recording the
relationship.

## 3. Run the helper

Example:

```powershell
.\providers\foundation_pose\tools\cad_prepare\prepare_model.ps1 `
    -InputObj C:\robot_cad\base.obj `
    -ModelId my_robot_base `
    -Role robot_base `
    -SemanticFrame robot/base_reference `
    -DefaultChildFrame observed_object/my_robot/base `
    -CoordinateUnits millimeters `
    -ScaleToM 0.001 `
    -SemanticFrameMode original_export
```

The helper writes persistent output under `config/foundation_pose`.

Generated files include:

- cleaned original-frame OBJ;
- centered FoundationPose OBJ;
- metadata with original/centered bounds;
- exact centering transform;
- model-registry entry.

### Reporting-frame modes

`-SemanticFrameMode centered_mesh` keeps the reporting frame at the generated
centered mesh origin and writes identity `mesh_from_semantic`.

`-SemanticFrameMode original_export` treats the preserved OBJ export frame as
the semantic/reporting frame and automatically converts the recorded centering
translation into metres for `mesh_from_semantic`.

`-SemanticFrameMode custom` requires `-MeshFromSemanticJson` and is intended
for a known robot kinematic or calibration frame.

## 4. Define mesh_from_semantic

FoundationPose returns `camera_from_mesh`.

The Provider publishes:

`camera_from_semantic = camera_from_mesh @ mesh_from_semantic`

If the centered mesh frame is not the desired reporting frame, define the
correct rigid transform.

Do not use this field to cosmetically remove estimator noise or symmetry
ambiguity.

## 5. Record units

`scale_to_m` must convert mesh coordinates to metres.

Examples:

- millimetres: `0.001`
- centimetres: `0.01`
- metres: `1.0`

## 6. Validate

Before using TRACK:

1. obtain one synchronized RGB-D frame;
2. create a clean visible-object mask;
3. run one ESTIMATE;
4. project the actual CAD surface back into RGB;
5. check metric depth, translation, and orientation;
6. only then start continuous tracking.

Symmetric geometry may legitimately produce multiple equivalent visual
orientations. Skills should resolve task-specific ambiguity when necessary.
