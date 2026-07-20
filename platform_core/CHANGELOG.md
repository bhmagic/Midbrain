# Changelog

## 0.2.0

- Adds Manager capability catalog at `GET /v1/capabilities`.
- Adds configurable provider heartbeat expiry and publishes unavailable state to the Fabric.
- Adds Fabric stream catalog at `GET /v1/streams` with age and freshness state.
- Adds freshness-aware timestamp-nearest multi-stream query at `GET /v1/sync`; incomplete required bundles return HTTP 409.
- Increases default per-stream history to support synchronization queries.

## 0.3.0

- Added native timestamped static/dynamic transform graph and schema discovery.
- Added interpolation, bounded extrapolation, session epochs, provenance, and authority conflict reporting.
- Added generic provider request forwarding.
- Added motion-inhibit acquire/status/release and Fabric publication.
- Added combined camera and Local VIO provider configuration template.
