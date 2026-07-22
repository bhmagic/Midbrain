# Bundled NVLabs FoundationPose model checkpoints

This directory contains the two pretrained model-based FoundationPose checkpoint
sets required by this Provider:

- `2023-10-28-18-33-37` — pose refiner
- `2024-01-11-20-02-45` — pose scorer

## Provenance

The files were manually downloaded on 2026-07-22 from the official NVLabs
FoundationPose weight folder linked by the upstream FoundationPose README:

`https://drive.google.com/drive/folders/1DFezOAD0oD1BblsXVxqDsl8fj0qzB82i?usp=sharing`

Upstream project:

`https://github.com/NVlabs/FoundationPose`

The filenames, byte sizes, and SHA-256 digests are recorded in
`WEIGHTS_MANIFEST.sha256`. The scorer checkpoint SHA-256 also matches the
publicly visible 190 MB FoundationPose scorer artifact hash
`81924d384bf5c26c646ee4783104982ae3d1e049c181c36641b6a7aeae494c26`.

## Installation behavior

The Provider installers use these bundled files first and do not need Google
Drive access. The files are copied to:

`providers/foundation_pose/nvlabs/FoundationPose/weights`

and also cached persistently under:

`config/foundation_pose/install_cache/nvlabs/FoundationPose/weights`

The persistent cache survives complete Provider deletion/reinstallation.

## Licensing note

These checkpoint files are third-party NVIDIA materials, not Midbrain code.
The upstream FoundationPose repository currently states that its code and data
are released under the NVIDIA Source Code License. The upstream license and
notices govern these files; the Midbrain MIT license does not replace those
terms.

The complete NVIDIA Source Code License is included beside this README. It
requires redistribution under that license with its text and notices retained,
and limits the work to non-commercial research or evaluation use.

This directory intentionally records provenance now so the final audit has a
clear origin trail.
