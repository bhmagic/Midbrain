# Third-Party Notices and License Audit Status

The original code authored for this repository is released under the MIT License. See `LICENSE`.

This repository also declares or interfaces with third-party packages, SDKs, drivers, protocols, and tools. Those components remain subject to their own copyright notices, licenses, and redistribution terms. The MIT License does not replace or override any third-party terms.

## Current status

A complete source-origin and dependency-license audit has not yet been completed. Before distributing compiled binaries, vendoring dependencies, copying external source, or bundling the Orbbec SDK/runtime, review and record the applicable licenses and notices.

Package-manager manifests and lock files identify many direct and transitive dependencies, but they are not a substitute for a license audit.

## Audit checklist

- Identify copied, adapted, generated, or vendored source and preserve its attribution and license text.
- Review Rust dependencies recorded in `platform_core/Cargo.lock`.
- Review Python direct and transitive dependencies from the three `pyproject.toml` files and built wheel metadata.
- Review Orbbec SDK, driver, firmware, extension, sample-code, and runtime redistribution terms separately.
- Confirm that images, calibration datasets, captures, documentation excerpts, and generated assets are owned or properly licensed.
- Add required notices here and place full third-party license texts under a future `third_party_licenses/` directory when required.

Until this audit is complete, source publication should not be represented as confirmation that every external component may be redistributed under MIT.
