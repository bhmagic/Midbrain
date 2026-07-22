# Publication checklist

- [x] Provider lifecycle is Manager-controlled.
- [x] Provider uses generic `perception.object_pose.*` capabilities.
- [x] Provider reads RGB-D from Fabric BufferRefs.
- [x] Pose/transform observations use camera acquisition timestamps.
- [x] Dynamic transforms include authority and session epoch.
- [x] Provider does not overwrite robot kinematic/fused-state authority.
- [x] Persistent robot configuration is outside the Provider directory.
- [x] Default Base and Gripper roles are explicit in the model registry.
- [x] Default reBot CAD provenance/source/license/modification notices retained.
- [x] NVLabs source checkout and compiled runtime artifacts are not committed to the Provider Git tree.
- [x] The two required NVIDIA checkpoint files are published through Git LFS with the complete governing license.
- [x] `.venv`, caches, logs, captures, backups, and runtime debug output excluded.
- [x] CAD preparation is an offline helper.
- [x] Native-Windows compatibility patch is guarded and tested.
- [x] Manager request envelope is supported.
- [x] Unit tests cover registry metadata, stable child frames, publication metadata,
      BOM handling, transforms, backend behavior, and Windows compatibility patch.

- [x] Static publication validator included.
- [x] Generic live Manager/Fabric validation script included.
