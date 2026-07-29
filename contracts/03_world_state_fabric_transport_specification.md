# World State Fabric Transport Specification

Version: 0.2 Working Draft

## 1. Scope

This specification defines how Resource Providers publish observations, subscribe to state metadata, and exchange references to large payloads through the World State Fabric.

The Fabric carries canonical state, observations, events, and access metadata. It does not carry same-machine video, RGB-D, point-cloud, audio-block, or large-tensor payloads when shared memory is available.

## 2. Transport layers

### Control and metadata

Preferred transport:

- Protocol Buffers
- gRPC unary calls and streams
- Loopback TCP as the portable local baseline

### Large payloads

Preferred local transport:

- Shared-memory pools
- Ring buffers
- Memory-mapped regions

Optional platform-specific optimizations may include GPU buffer sharing. They must preserve the same reference and lifetime semantics.

### Remote fallback

When producer and consumer are not on the same host, the Fabric may expose a separate remote payload transport using chunked or compressed streams. Remote transport is not required for version 0.1.

## 3. Observation envelope

Every observation must contain:

- Schema identifier
- Schema version
- Provider identifier
- Provider instance identifier
- Provider boot identifier
- Stream identifier
- Sequence number
- Observation timestamp
- Fabric arrival timestamp
- Clock domain
- Coordinate frame when applicable
- Calibration revision when applicable
- Confidence
- Validity
- Producer-declared hard expiry and optional recommended age
- Related Skill or request identifier when applicable
- Inline payload or `BufferRef`

A provider restart must create a new boot identifier. Sequence numbers only need to be monotonic within one boot and stream.

## 4. BufferRef

A `BufferRef` must contain:

- `pool_id`
- `slot_id`
- `generation`
- `offset_bytes`
- `length_bytes`
- `format`
- `shape`
- `stride`
- `frame_id` or `sample_id`
- `observation_time`
- `producer_id`
- `sensor_id` when applicable
- `calibration_revision` when applicable
- `lease_expiry`
- `integrity_state`

Optional fields:

- Device location such as host memory or GPU
- Compression method
- Checksum
- Plane descriptions for multi-plane formats
- Synchronization group identifier

A `BufferRef` is metadata. It is not ownership of the underlying memory.

## 5. Pool registration

Before publishing references, a producer must register a pool with the Fabric.

Pool registration includes:

- Pool identifier
- Producer instance and boot identifiers
- Shared-memory discovery information
- Total size
- Slot count
- Slot layout
- Supported formats
- Access permissions
- Lifetime policy
- Producer crash policy

The Fabric distributes discovery information only to authorized consumers.

## 6. Slot and generation rules

Each reusable slot has a generation number.

A producer must change the generation whenever the slot is reused for a new payload.

A consumer must reject the reference when:

- The pool is unknown
- The slot is out of range
- The current generation differs
- Offset or length exceeds the pool bounds
- The lease has expired
- The producer boot identifier is obsolete
- The integrity state is invalid

Generation checks prevent a recycled slot from being mistaken for an older frame.

## 7. Commit sequence

A producer should follow this order:

1. Reserve a writable slot.
2. Mark it as being written.
3. Write the payload.
4. Write metadata.
5. Commit the generation and integrity state atomically or with an equivalent synchronization rule.
6. Publish the `BufferRef` observation.

The reference must never be published before the payload is committed.

## 8. Consumer access

The Fabric must support these passive metadata queries:

- Latest reference for a stream, with structural-validity and temporal metadata
- Reference by frame or sample identifier
- Reference nearest a timestamp
- Synchronized references for related streams within a consumer-requested
  association tolerance
- References within a recent time window

A consumer should:

1. Receive or query a reference.
2. Validate the reference.
3. Acquire a short read lease when the pool requires it.
4. Read the payload.
5. Release the lease promptly.

Consumers must not retain references indefinitely.

## 9. Ring-buffer policy

Each pool must declare:

- Slot count
- Maximum payload size
- Overwrite policy
- Read-lease policy
- Maximum read-lease duration
- Behavior when no slot is immediately writable

Recommended default for RGB-D streams:

- Bounded ring buffer
- Latest data preferred
- Shallow history
- Oldest non-leased slot overwritten first
- Capture never blocked indefinitely by visualization or slow analytics

A short history should be retained long enough for a prewarmed provider to inspect frames immediately before a triggering event. The exact duration is deployment-specific.

## 10. Synchronization

RGB and depth references should include:

- Individual observation timestamps
- Shared synchronization group identifier when captured together
- Maximum synchronization error
- Calibration revision

The Fabric may return, using a tolerance supplied by the consumer:

- Exact synchronized pair
- Nearest pair within a requested tolerance
- No valid pair

It must not silently return a pair outside the requested association
tolerance. This association decision does not assert that the data will still
be fresh after a Skill finishes computation.

## 11. Temporal evidence and canonical state

The Fabric owns canonical state revisions.

A provider publishes observations, not authoritative replacement of canonical state.

The Fabric may reject or ignore an observation because it is:

- Out of sequence
- From an obsolete provider boot
- Unauthorized
- Structurally invalid
- Based on unknown calibration
- Outside requested synchronization tolerance

The Fabric preserves raw observations even after their producer-declared hard
expiry when history policy permits. It reports hard expiry, invalidity,
provider-boot incompatibility, BufferRef generation loss, and authority
revocation without erasing the historical record.

Every canonical state value retains source observation time, Fabric arrival
time, clock domain, provider instance and boot, sequence, confidence,
structural validity, and producer temporal recommendations. The Fabric must
not convert a producer-recommended age into one universal Skill acceptance
policy.

Each consuming Skill declares its own maximum source age, association
tolerance, allowed extrapolation, required epoch/revision, and post-compute
continuity policy. A slow VLM or neural inference records its input observation
time separately from inference start and completion; publication of a new
result must not make the source observation appear newer.

## 12. Producer crash

When a producer crashes or disconnects:

- Its pool registration becomes unavailable for new references.
- Existing references remain valid only until their original expiry and only if the memory still exists.
- The Fabric reports that the producer boot or pool is unavailable.
- Consumers apply their own temporal policy and must not assume the last frame
  remains usable for a new live operation.

The Manager may restart the provider with a new boot identifier and a new pool registration.

## 13. Consumer crash

Read leases must expire automatically. A crashed consumer must not permanently reserve slots.

The producer may reclaim slots after lease expiry.

## 14. Backpressure

Each stream must declare a delivery policy:

- Latest only
- Bounded ordered queue
- Reliable event delivery

Recommended defaults:

- RGB-D references: latest or shallow bounded queue
- Robot joint state: bounded low-latency stream
- Safety events: reliable and acknowledged
- Visualization: droppable and non-blocking

No visualization or logging consumer may block safety or physical-control state publication.

## 15. Security

Pool discovery information must be treated as an access authorization to shared memory.

The Fabric must restrict access by provider identity and permission. Shared-memory names, handles, or tokens must not be globally exposed without access control.

## 16. Version 0.1 minimum

Version 0.1 must implement:

- Observation envelope
- Local pool registration
- `BufferRef`
- Slot generation validation
- Expiring read leases or an equivalent safe reclamation rule
- Latest-frame query
- Timestamp-nearest query
- RGB/depth synchronization query
- Producer crash invalidation
- Consumer crash reclamation
- Bounded backpressure behavior
