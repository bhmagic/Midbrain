# Changelog

## 0.3.1 - 2026-07-29

- Added an idempotent local signing-secret bootstrap for reviewed workcell
  activation and decision-specific physical execution assertions. Example
  environment files document the required variables without containing local
  secret values.
- Added a bounded workspace launcher preflight that rejects Manager or Fabric
  release executables older than their applicable Rust source, crate
  manifests, root Cargo files, toolchain file, or build scripts.
- Kept process startup bounded and headless-capable so callers can impose
  explicit readiness and idle-time limits instead of waiting indefinitely on
  a foreground workspace process.
- Physically validated the enforced reviewed-workcell activation path as part
  of the final Agents SDK observation-motion test. The active transform
  retained exact camera boot, calibration revision, VIO epoch, quality,
  reviewer, digest, and expiry provenance.
- Revalidated the Manager-owned ordered shutdown plus local authoritative arm
  fallback after the Agent SDK checkpoint. Safe-home completed before
  Integrated, Basic, camera, VIO, FoundationPose, Manager, Fabric, and Test
  Agent shutdown.

## 0.3.0

- Added capability-binding coverage that preserves a cold explicit provider
  fallback as `FALLBACK_REQUIRES_ACTIVATION` and promotes that same provider to
  a `CURRENT` advertised selection only after independent activation.
- Added native timestamped static/dynamic transform graph and schema discovery.
- Added interpolation, bounded extrapolation, session epochs, provenance, and authority conflict reporting.
- Added generic provider request forwarding.
- Added motion-inhibit acquire/status/release and Fabric publication.
- Added combined camera and Local VIO provider configuration template.
- Added per-provider `force_kill_on_stop_timeout` policy. It defaults to
  enabled for compatibility and may be disabled for safety-critical providers
  that must retain powered support after a graceful-stop timeout.
- Added enforced reviewed-workcell activation with signed reviewer identity,
  current camera/VIO provenance, quality and lifetime gates, one-active
  policy, idempotency, and explicit revocation.
- Fabric now treats a newer Manager revocation as authoritative over an older
  static transform observation.

## 0.2.0

- Adds Manager capability catalog at `GET /v1/capabilities`.
- Adds configurable provider heartbeat expiry and publishes unavailable state to the Fabric.
- Adds Fabric stream catalog at `GET /v1/streams` with age and freshness state.
- Adds freshness-aware timestamp-nearest multi-stream query at `GET /v1/sync`; incomplete required bundles return HTTP 409.
- Increases default per-stream history to support synchronization queries.
