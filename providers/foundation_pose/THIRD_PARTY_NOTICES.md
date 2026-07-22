# Third-party notices

This Provider combines original Midbrain integration code with optional external
software and default robot geometry that retain their own licenses.

## Meta Segment Anything 2

The optional GUI initialization path uses the official Meta SAM2 source under
the Apache License 2.0. The source and checkpoint are installed locally by
`scripts/setup_sam2.ps1`; neither is included in the Provider publication zip.

Upstream project: https://github.com/facebookresearch/sam2

Pinned revision: `2b90b9f5ceec907a1c18123530e92e794ad901a4`

Model: SAM2.1 Hiera Base+

Checkpoint SHA-256:
`a2345aede8715ab1d5d31b4a509fb160c5a4af1970f199d9054ccfb746c004c5`

## NVLabs FoundationPose

Project:

https://github.com/NVlabs/FoundationPose

The Provider does **not** vendor the NVLabs FoundationPose source, CUDA
libraries, PyTorch, nvdiffrast, PyTorch3D, or other native dependencies.

The v0.2.4 offline distribution **does bundle** the two official model-based
FoundationPose checkpoint sets required by the Provider. They are kept under
`third_party/nvlabs_foundationpose_weights` with a provenance note and exact
SHA-256 manifest.

The files were manually downloaded on 2026-07-22 from the official FoundationPose
Google Drive folder linked by the upstream README. They remain NVIDIA
third-party materials; the Midbrain MIT license does not replace NVIDIA's terms.

The upstream FoundationPose repository states that its code and data are
released under the NVIDIA Source Code License. The complete license text is
included at:

`third_party/nvlabs_foundationpose_weights/NVIDIA_SOURCE_CODE_LICENSE.txt`

That license permits redistribution only under its own terms and limits the
work to non-commercial research or evaluation use. The bundled checkpoints are
not relicensed under Midbrain's MIT license.

The Provider applies one guarded local compatibility patch on native Windows:
the upstream temporary centered-OBJ path is changed from a Linux-only `/tmp`
path to `FOUNDATIONPOSE_TEMP_DIR` or Python's platform temporary directory.
The patch is applied only to the pinned, recognized upstream source shape.

## Seeed Studio reBot-DevArm hardware geometry

Project:

https://github.com/Seeed-Projects/reBot-DevArm

Default robot:

Seeed reBot Arm B601-DM

Upstream commit used for the prepared geometry:

`0d74520357b46be02e07104c0d1bbb4e46789aef`

The hardware design is licensed under CERN-OHL-W-2.0.

The Provider ships selected source STEP files and derived prepared OBJ files
under:

`defaults/rebot_b601_dm`

The full retained license text is:

`defaults/rebot_b601_dm/licenses/CERN-OHL-W-2.0.txt`

Modification notice:

`defaults/rebot_b601_dm/MODIFICATIONS.md`

Source/provenance notice:

`defaults/rebot_b601_dm/UPSTREAM.md`

The Midbrain Provider code is external software interfacing with these hardware
geometry assets and remains under the Midbrain MIT license.
