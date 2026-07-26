# Modification notice for reBot B601-DM CAD derivatives

Date: 2026-07-21

The following changes were made to selected Seeed Studio reBot B601-DM
hardware geometry for use as rigid FoundationPose targets:

## Base target

Upstream source components:

- `01_BASE_Plate.step`
- `01_BASE_Link.step`

Preparation:

1. The two rigid base components were isolated from the articulated assembly.
2. They were exported to `Base.obj` in millimetres.
3. The mesh received minimal duplicate-vertex cleanup and normal recalculation.
4. `Base_clean_original_frame.obj` preserves the exported rigid-target frame.
5. `Base_clean_centered.obj` subtracts the original-frame bounding-box center.
6. The exact centering transform is recorded in `Base_mesh_metadata.json`.

## Gripper target

Upstream source component:

- `01_Rail_Bracket.step`

Preparation:

1. The rigid Gripper Slider Support / Rail Bracket was isolated.
2. It was exported to `Gripper.obj` in millimetres.
3. Minimal duplicate-vertex cleanup and normal recalculation were applied.
4. `Gripper_clean_original_frame.obj` preserves the exported frame.
5. `Gripper_clean_centered.obj` subtracts the original-frame bounding-box center.
6. The exact centering transform is recorded in `Gripper_mesh_metadata.json`.

The preparation helper is included under `tools/cad_prepare/`.
