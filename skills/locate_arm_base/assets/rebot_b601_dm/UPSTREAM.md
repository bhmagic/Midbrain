# reBot B601-DM CAD provenance

This file is one of two intentional restore-profile mirrors at
`config/foundation_pose` and
`providers/foundation_pose/defaults/rebot_b601_dm`. Keep the copies
byte-identical; configuration validation enforces the mirror.

The default robot geometry in this directory is derived from the Seeed Studio
`reBot-DevArm` hardware repository.

- Upstream: https://github.com/Seeed-Projects/reBot-DevArm
- Upstream commit used for this preparation: `0d74520357b46be02e07104c0d1bbb4e46789aef`
- Hardware license: CERN-OHL-W-2.0
- Robot profile: reBot Arm B601-DM

Source hardware files retained in this directory:

- `source/01_BASE_Plate.step`
- `source/01_BASE_Link.step`
- `source/01_Rail_Bracket.step`

The prepared OBJ files are derivative geometry intended for FoundationPose
inference. See `MODIFICATIONS.md` and `licenses/CERN-OHL-W-2.0.txt`.
