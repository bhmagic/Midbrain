# CAD preparation helper

These tools are optional and are not used by the running Provider. They exist
to prepare a new rigid CAD target when replacing the default reBot B601-DM
geometry.

## Important rule

FoundationPose targets must be rigid. Do not export an articulated robot arm as
one object. Select rigid references independently, for example a base housing,
a wrist bracket, or a gripper support.

## Supported preparation path

1. Isolate one rigid target in the robot CAD tool.
2. Export that target as OBJ while preserving a meaningful original frame.
3. Run `prepare_model.ps1`.
4. Review the generated original-frame mesh, centered mesh, metadata, and
   `mesh_from_semantic`.
5. Use the centered mesh for FoundationPose.
6. Keep robot/task-specific frame disambiguation outside the Provider when the
   geometry is symmetric or semantically ambiguous.

The helper currently expects OBJ input. STEP assemblies should first be
isolated/exported with FreeCAD or another CAD tool.

## VLM reference rendering

Generate VLM reference images once with Blender Workbench and store the finished
PNG atlases with the model profile. The Blender path uses every mesh face and a
real depth buffer. Do not use triangle subsampling or a painter's-algorithm
projection for CAD reference images.

```powershell
& "C:\Program Files\Blender Foundation\Blender 5.0\blender.exe" `
    --background `
    --factory-startup `
    --python .\providers\foundation_pose\tools\cad_prepare\render_reference_blender.py `
    -- `
    --input .\providers\foundation_pose\defaults\rebot_b601_dm\models\Base_clean_centered.obj `
    --output .\providers\foundation_pose\defaults\rebot_b601_dm\references\Base_reference_atlas.png `
    --label "reBot B601-DM Base" `
    --scale-to-m 0.001 `
    --ortho-scale 0.30
```

The renderer creates an eight-view `2048x1024` atlas with one shared
orthographic scale for every view. Pass the same explicit `--ortho-scale` to
related model atlases when their physical relative size should also be
preserved. It overrides global OBJ smoothing for the
reference render, uses flat face normals, and disables Workbench cavity,
specular, and shadow styling so hard mechanical surfaces are not shown as
inflated. A JSON sidecar records the Blender version, mesh and output hashes,
vertex/face counts, scale, view angles, and `face_sampling: false`. The running
GUI loads the saved atlases; it does not render CAD references on demand.

## Example

```powershell
.\providers\foundation_pose\tools\cad_prepare\prepare_model.ps1 `
    -InputObj C:\robot_cad\base.obj `
    -ModelId my_robot_base `
    -Role robot_base `
    -SemanticFrame robot/base_reference `
    -DefaultChildFrame observed_object/my_robot/base `
    -Description "Rigid visual base reference" `
    -CoordinateUnits millimeters `
    -ScaleToM 0.001 `
    -SemanticFrameMode original_export
```

### Reporting-frame modes

`-SemanticFrameMode centered_mesh` keeps the reporting frame at the generated
centered mesh origin and writes identity `mesh_from_semantic`.

`-SemanticFrameMode original_export` treats the preserved OBJ export frame as
the semantic/reporting frame and automatically converts the recorded centering
translation into metres for `mesh_from_semantic`.

`-SemanticFrameMode custom` requires `-MeshFromSemanticJson` and is intended
for a known robot kinematic or calibration frame.

By default, `mesh_from_semantic` is identity. If the centered mesh frame is not
the intended semantic frame, provide a JSON file containing the 16-value
`mesh_from_semantic` matrix with `-MeshFromSemanticJson`.

All generated user data is written to `config/foundation_pose`, outside the
Provider directory, so Provider upgrades can safely replace
`providers/foundation_pose`.
