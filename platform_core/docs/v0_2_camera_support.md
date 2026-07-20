# Core 0.2 Support for Full-Capability Cameras

The Femto Bolt expansion required two general core features.

## Manager capability catalog

Providers publish `details.capability_readiness` in heartbeats. The Manager exposes those values at `/v1/capabilities` and combines them with lifecycle, health, and heartbeat-expiry state. Agent adapters and Skills can discover `camera.depth`, `camera.ir`, or `camera.point_cloud.xyz` without binding to a specific Provider ID.

## Fabric stream catalog and synchronized lookup

`/v1/streams` exposes active stream metadata and staleness. `/v1/sync` returns freshness-filtered timestamp-nearest observations for requested streams using bounded history and rejects incomplete required bundles with HTTP 409. This supports RGB-depth-IMU selection without copying image or point-cloud payloads into the Fabric.

## Deliberate boundary

The Fabric still carries BufferRefs, calibration, timing, and small state. It does not continuously copy, align, or fuse the large payloads. Derived products such as aligned depth and point clouds remain Provider outputs and can be disabled when their resource cost is not needed.
