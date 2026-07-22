# Third-Party Notices and License Audit Status

The original code authored for this repository is released under the MIT License. See `LICENSE`.

This repository also declares or interfaces with third-party packages, SDKs, drivers, protocols, and tools. Those components remain subject to their own copyright notices, licenses, and redistribution terms. The MIT License does not replace or override any third-party terms.

## Current status

A complete source-origin and dependency-license audit has not yet been completed. Before distributing compiled binaries, vendoring dependencies, copying external source, or bundling the Orbbec SDK/runtime, review and record the applicable licenses and notices.

Package-manager manifests and lock files identify many direct and transitive dependencies, but they are not a substitute for a license audit.

## FoundationPose Provider notices

The FoundationPose Provider integrates with the NVLabs FoundationPose project and publishes two required NVIDIA checkpoint files through Git LFS. The complete governing license is included at `providers/foundation_pose/third_party/nvlabs_foundationpose_weights/NVIDIA_SOURCE_CODE_LICENSE.txt`. Those materials are licensed for non-commercial research and evaluation only; they are not covered by the Midbrain MIT License.

The prepared Base and Gripper geometry and reference renders originate from reBot B601/ER1.6 CAD material. The corresponding CERN-OHL-W-2.0 license is retained at `providers/foundation_pose/defaults/rebot_b601_dm/licenses/CERN-OHL-W-2.0.txt`, with attribution and modification notices beside the prepared profile.

The Provider can optionally use SAM2 and the OpenAI API during GUI-assisted mask initialization. Their code, model files, hosted services, and outputs remain subject to their respective licenses and service terms and are not relicensed by this repository.

## Audit checklist

- Identify copied, adapted, generated, or vendored source and preserve its attribution and license text.
- Review Rust dependencies recorded in `platform_core/Cargo.lock`.
- Review Python direct and transitive dependencies from the three `pyproject.toml` files and built wheel metadata.
- Review Orbbec SDK, driver, firmware, extension, sample-code, and runtime redistribution terms separately.
- Confirm that images, calibration datasets, captures, documentation excerpts, and generated assets are owned or properly licensed.
- Add required notices here and place full third-party license texts under a future `third_party_licenses/` directory when required.

Until this audit is complete, source publication should not be represented as confirmation that every external component may be redistributed under MIT.
