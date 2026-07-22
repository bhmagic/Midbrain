# Installation policy

Persistent robot-specific CAD, mesh metadata, masks, captures, and model registry belong under:

`config/foundation_pose/`

Every Provider revision is distributed as a complete FoundationPose Provider ZIP.

## Fast overwrite/update

Use the fast update path when the existing Provider-local `.venv`, pinned NVLabs checkout, and native CUDA builds are healthy. The current offline package can repair missing runtime weights from its bundled payload.

A fast update must:

1. Stop the Midbrain workspace.
2. Verify ports 7001, 7002, 7101, 7102, and 7103 are not listening.
3. Preserve `config/foundation_pose/`.
4. Preserve `providers/foundation_pose/.venv/` and `providers/foundation_pose/nvlabs/`.
5. Replace the complete Provider code/config/schema/scripts tree from the full revision ZIP.
6. Refresh the editable Provider package in the existing `.venv`.
7. Apply and verify revision-specific NVLabs compatibility patches.
8. Replace the Provider registration entry by `id`.
9. Run Provider tests and the native backend smoke test.
10. Leave the workspace stopped.

## Complete clean reinstall

Use the clean path whenever dependencies, native builds, CUDA/PyTorch compatibility, or the NVLabs checkout must be recreated.

A clean installation must:

1. Stop the Midbrain workspace.
2. Verify ports 7001, 7002, 7101, 7102, and 7103 are not listening.
3. Preserve `config/foundation_pose/`.
4. Remove `providers/foundation_pose/` completely.
5. Install a fresh Provider tree and `.venv`.
6. Install a fresh pinned NVLabs FoundationPose checkout and native dependencies.
7. Apply and verify revision-specific NVLabs compatibility patches.
8. Replace the Provider registration entry by `id`.
9. Run Provider tests and the native backend smoke test.
10. Leave the workspace stopped.

## Publication default profile

The Git tree contains Provider-owned default assets under
`defaults/rebot_b601_dm`. Installation seeds missing CAD/source files into persistent
`config/foundation_pose` without overwriting existing geometry. For a registry
that is recognized as the default reBot profile, setup may refresh only the
publication metadata (`role`, `description`, and stable `default_child_frame`)
and add a missing default reporter while preserving existing mesh paths, frame
transforms, scale, and model revisions. Custom registries are left unchanged
unless an explicit force option is requested.

Recorded captures, user masks, installation reports, and backups remain
machine-local and are not part of the Provider Git tree.


## Third-party checkpoint cache

A successful installation stores validated copies of the two required
FoundationPose checkpoint sets under
`config/foundation_pose/install_cache/nvlabs/FoundationPose/weights`.

The offline Provider ZIP also contains the checkpoint payload under
`third_party/nvlabs_foundationpose_weights/weights`. The installer verifies the
bundle by SHA-256 and populates both the runtime and persistent cache without
network access.

The cache remains outside the disposable NVLabs runtime and survives complete
Provider reconstruction.
