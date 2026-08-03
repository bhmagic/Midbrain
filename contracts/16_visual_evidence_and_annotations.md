# Visual Evidence and Annotation Contract

Status: v0.1 compatibility working draft.

## Purpose

This contract lets an Agent show the exact image used by a visual Skill plus
structured, interactive annotations without requiring the Skill to burn those
annotations into a second raster image. It is independent of the VLM,
perception runtime, camera Provider, and browser rendering library.

The browser currently renders annotations as SVG. SVG is a presentation choice,
not the contract boundary. A native UI or replay tool may render the same
geometry differently.

## Evidence identity and channels

One evidence object has a unique `evidence_id`, a bounded set of channels, a
default channel, provenance, and zero or more annotations. Each channel has:

- a stable channel ID such as `rgb`, `depth`, `ir`, or `mask`;
- an exact URL for the retained evidence bytes;
- media type, width, height, and SHA-256 digest; and
- a human-readable label.

The first implementation retains exact RGB evidence in process memory for the
same development-scale period as Agent event replay. It does not substitute a
new `latest` camera frame when replaying an annotation. A service restart or
retention expiry may make the URL unavailable; durable field deployment must
move both event and artifact retention behind a persistent implementation.

## Annotation coordinates

Version 1 uses normalized image coordinates with top-left origin, positive X
right, and positive Y down. Geometry is therefore independent of raster
resolution. Every annotation declares the channels to which it applies. The UI
must hide an annotation on a selected channel that is not in that list.

The initial implemented geometry is:

- `point`: normalized `x` and `y`;
- `box`: normalized top-left `x`, `y`, `width`, and `height`.

Future compatible schema revisions may add lines, polygons, masks, keypoints,
and projected 3D pose axes. Producers must not encode such geometry as a label
or as arbitrary SVG/HTML.

Model adapters may accept a known upstream coordinate convention and must
convert it to normalized `0..1` geometry before producing this contract. The
initial adapter accepts normalized `0..1`, explicitly declared pixel geometry
when the captured image dimensions are available, and the normalized `0..1000`
convention used by Gemini Robotics-ER. A model-specific hint may identify the
last form when the response omits its coordinate-space field; generic values
greater than `1` are not guessed as pixels. Invalid, ambiguous, out-of-range,
or unsupported annotations are dropped at both the Skill-output and
browser-event boundaries.

The Skill result includes bounded `annotation_processing` diagnostics with
input, accepted, rejected, and truncated counts, accepted coordinate spaces,
and rejection reasons. Those diagnostics are operational metadata and are not
part of the visual-evidence browser projection. Raw annotation payloads are not
included in diagnostics.

## UI behavior

An observer may:

- switch among available evidence channels;
- show or hide applicable annotations;
- use deterministic, distinct local display colors for annotations;
- override each annotation color independently and reset the palette; and
- copy or download a client-side flattened image.

Changing display state does not modify the retained source evidence or the
annotation record. A flattened export is a user convenience and is not the
authoritative evidence artifact. The channel digest continues to identify the
unmodified source image.

Display colors are browser state rather than inference output. The same local
colors must be used by the live SVG and flattened export, and they remain
stable when switching evidence channels. Color does not encode confidence or
motion authority unless a future contract version explicitly defines that
semantic.

## Agent event projection

A tool result may contain a `visual_evidence` object conforming to
`schemas/visual_evidence.v1.schema.json`. The SDK adapter emits only a validated
and allowlisted projection as `visual.evidence.created`; raw tool output,
filesystem paths, prompts, credentials, and arbitrary URLs remain excluded.

Visual evidence is observation, not motion authority. Confidence labels and
VLM annotations do not replace registered depth, calibrated transforms,
controller validation, leases, fencing, or safety policy.

## Security and privacy

Camera images can contain sensitive workcell or environmental information.
The development implementation remains loopback-bound by default and serves
artifacts with `no-store`. Before remote exposure, image access and event access
must share the authenticated, role-authorized command/observation boundary
described in the security roadmap.
