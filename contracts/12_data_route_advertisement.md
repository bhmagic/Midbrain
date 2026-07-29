# Provider Data-Route Advertisement

Version: 0.1 Working Draft

## Scope

This contract lets a Resource Provider advertise a direct data path without
making the World State Fabric part of the payload's latency-sensitive path.
The Fabric carries discovery and provenance metadata. The advertised transport
carries the large payload.

## Selection policy

A route descriptor declares whether it is generic or hardware-specific. A
hardware-specific route may remain available as a compatibility fallback after
a generic RGB-D format is introduced.

Consumers should prefer a compatible generic route when one is available.
They may select a hardware-specific route when the generic route is absent,
incomplete, too slow for the operation, or explicitly selected by provider ID.
The selected route and provider identity must be retained in the consumer's
request or audit record.

## Provider obligations

A provider exposing a direct route must:

- advertise a capability-readiness entry through Manager heartbeats;
- publish a `physical_agent.data_route` observation to the Fabric;
- identify the provider instance and boot that owns the transport;
- state whether the route is currently available;
- describe reference validation and calibration provenance;
- republish often enough for an in-memory Fabric restart to rediscover it;
- publish an unavailable update before a normal transition out of service when
  practical.

The advertisement must not contain credentials or bypass authorization. Route
discovery does not grant permission to use its data.

## Flexible RGB-D shared-memory route

The generic route capability is
`camera.rgbd.route.generic_shared_memory`. Its payload remains in provider
shared memory. Fabric carries only timestamps, recyclable buffer references,
per-channel geometry, calibration revision, alignment metadata, and other
small numeric or text status.

The route treats RGB, infrared, raw depth, and registered depth as independent
channels. A consumer must not assume equal width, height, stride, aspect ratio,
crop, valid boundary, intrinsics, timestamp, or coordinate frame. Each channel
declares its native grid and valid region.

An alignment record names its source, target, and output channels. Providers
may write a custom registered product directly into shared memory and
advertise it as `PROVIDER_CUSTOM`; the record declares the output grid,
calibration/extrinsic provenance, invalid-sample policy, and valid boundary.
The contract explicitly supports source and target channels whose resolutions,
aspect ratios, and image boundaries do not match.

The transport adapter can remain provider-specific while the channel and
alignment semantics are generic. This allows the current Femto Bolt named
mapping to coexist with later webcam or RGB-D transports without copying large
frames through Fabric.

## Orbbec Phase 1 route

The Femto Bolt provider advertises
`camera.femto_bolt.rgbd.windows_shared_memory.v2` on
`camera.rgbd.data_routes`. It is hardware-specific and marked
`COMPATIBILITY_FALLBACK`. Its payload remains in the existing Windows named
shared-memory mapping. RGB, depth, synchronized bundle, and calibration
observations remain available through their existing Fabric streams.

This Phase 1 contract does not move device calibration out of the Orbbec
provider and does not require existing Orbbec-aware consumers to use a generic
camera adapter.

## Orbbec Phase 2 coexistence

The Femto Bolt provider also advertises
`camera.rgbd.shared_memory.flexible.v1` as the preferred generic-semantic
route. Its current transport adapter still understands the Orbbec mapping
layout. RGB, infrared, and depth retain their native grids. The existing
software D2C output is exposed as provider-custom
`depth_registered_to_rgb`, including its own grid and boundary metadata.

The hardware-specific Phase 1 route remains advertised as
`COMPATIBILITY_FALLBACK` and may be selected explicitly by provider ID.
