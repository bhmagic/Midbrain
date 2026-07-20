# Current Core Progress

Snapshot version: 0.2.0
Role: expandable local Resource Provider hosting and observation infrastructure

## Resource Provider Manager now implements

- Static provider configuration and environment-variable expansion.
- Start, HOT, WARM, graceful stop, and force-kill endpoints.
- Provider registration and heartbeat reports.
- Capability-specific availability catalog at `/v1/capabilities`.
- Heartbeat expiry with explicit unavailable publication to the Fabric.
- Provider stdout/stderr forwarding and automatic startup.
- Initial Windows process-group termination behavior.

## World State Fabric now implements

- Single and batched observation ingestion.
- Latest value, bounded history, and full snapshot queries.
- Stream discovery with age, freshness, and stale indicators.
- Freshness-aware timestamp-nearest multi-stream bundles from retained history, with explicit incomplete-bundle status.
- Basic monotonic-sequence rejection per provider instance and boot.
- BufferRef metadata storage without copying large camera payloads.

## Important remaining Manager work

- Windows Job Objects and reliable child-tree ownership.
- Restart backoff, crash-loop detection, and policy-driven recovery.
- Formal provider manifests, contract negotiation, dependency resolution, and provider selection.
- Resource measurement, admission, priority, and GPU/VRAM arbitration.
- Authentication, permissions, signed packages, sandboxing, and remote providers.
- Command IDs, deadlines, idempotency, progress, and cancellation.
- Control Authority Leases, fencing generations, and safe relinquish for actuators.

## Important remaining Fabric work

- Schema registry and compatibility validation.
- Active stale-state invalidation rather than catalog annotation only.
- Clock domains, synchronization uncertainty, and timestamp conversion.
- Native timestamped transform graph.
- Buffer registry with acquire/release leases and producer-death invalidation.
- Event subscriptions and compact change notifications.
- Recording, replay, persistence, authority resolution, uncertainty, and fusion.
- Authentication, remote transport, and distributed deployment.

## Current conclusion

The basic local Provider-hosting gap is closed at prototype level. New sensor or model Providers can use the same lifecycle, capability, observation, and BufferRef boundaries. Remaining work improves reliability, discovery, temporal correctness, security, transforms, and physical-control safety rather than replacing the architecture.
