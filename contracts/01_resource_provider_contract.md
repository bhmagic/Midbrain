# Resource Provider Contract

Version: 0.2 Working Draft

## 1. Purpose

This contract defines the minimum behavior required for a **Resource Provider** to run under the **Resource Provider Manager** and exchange state metadata with the **World State Fabric**.

A **Resource Provider** is a persistent, hot, warm, or stopped service that provides computation, data, hardware access, control, recording, GUI, or visualization.

A **Skill** is a finite task execution that may use one or more Resource Providers and then finish.

The Manager owns process lifecycle, admission, fallback selection, resource scheduling, and physical-control authority. The Fabric owns canonical state and distribution of metadata, including references to large shared-memory payloads.

## 2. Required interfaces

Every Resource Provider must expose:

- Registration and version negotiation
- Status and heartbeat
- Residency change requests
- Request execution and cancellation
- Graceful stop
- Structured failure reporting
- Resource profile reporting
- Fabric publication and subscription interfaces
- Control-authority handling when physical control is provided

The preferred control protocol is Protocol Buffers over gRPC. A different transport may be used only when it preserves the same semantics.

## 3. Manifest

Every Resource Provider must provide a machine-readable manifest before launch.

Required fields:

- Provider identifier and version
- Supported contract versions
- Functions provided
- Input and output schema identifiers
- Supported operating systems and CPU architectures
- Executable, arguments, environment, and working-directory requirements
- Required files, models, devices, permissions, and dependencies
- GUI or interactive-session requirements
- Supported residency modes
- Resource profile
- Expected transition times
- Fabric streams consumed and published
- Physical-control resources required
- Compatible fallback providers or fallback classes

Unknown values must be represented explicitly as `UNKNOWN`, not as zero or an empty string.

## 4. Residency and process lifecycle

Stable residency modes are:

- `COLD`: no provider process is running.
- `WARM`: the process remains running but releases protected physical-control handles, high-rate processing, and most optional compute resources.
- `HOT`: initialization and declared warm-up have completed, and the provider is intended to respond without another heavy startup step.

Transition states may be reported as:

- `STARTING`
- `GOING_WARM`
- `WAKING`
- `STOPPING`

A provider being busy is not a residency mode. It may remain `HOT` while handling one or more requests.

The Manager owns process launch and forced process-tree termination. Forced kill is not a provider command.

### 4.1 Entering WARM

Before reporting `WARM`, a provider must:

- Stop accepting new incompatible work
- Complete or cancel active work according to the Manager deadline
- Release all physical-control authority
- Stop actuator commands
- Release high-rate subscriptions that are not needed while warm
- Release most optional RAM and VRAM when practical
- Release exclusive devices unless explicitly allowed to retain them
- Report resources intentionally retained
- Continue a minimal heartbeat and control connection

A provider may retain a compact checkpoint, configuration, small caches, and minimal runtime state.

### 4.2 Entering HOT

Before reporting `HOT`, a provider must:

- Reacquire required resources and permissions
- Reconnect required Fabric streams
- Validate required dependencies
- Validate calibration and device state when applicable
- Complete declared warm-up or self-test steps
- Report readiness for each provided capability; provider-level readiness must not hide a missing required capability

### 4.3 Graceful stop

On graceful stop, a provider must:

- Reject new work
- Cancel or complete current work within the deadline
- Release all authority and exclusive resources
- Flush required state and diagnostics
- Close Fabric publications
- Exit

If the deadline expires, the Manager may fence the provider, revoke authority, and terminate its complete process tree.

## 5. Fast-reaction behavior

A function that may be needed with low latency should not depend on launching all supporting providers from `COLD` after the request arrives.

Each provider must declare, when known:

- Cold-to-hot time
- Warm-to-hot time
- Hot-to-warm time
- Graceful-stop time
- Recommended idle timeout
- Minimum useful residency time
- Suggested prewarm events
- Related providers that are commonly needed at the same time

Values may be `MEASURED`, `ESTIMATED`, or `UNKNOWN`.

The Manager may keep likely-needed providers in `WARM` or `HOT` state based on task context, recent use, resource budget, and Fabric events. Related providers should be activated concurrently unless a dependency requires ordering.

Skills should request task-level behavior. For example, a pointing Skill may use a hand-landmark provider, an RGB-D fitting provider, recent frame references, and object state. The Skill finishes; supporting Resource Providers may remain warm.

## 6. Resource profile

For each supported residency mode, the provider should report the following when known:

- RAM reservation, expected use, and limit
- VRAM reservation, expected use, and limit
- CPU expected and peak normalized cores
- Installed size, persistent storage, cache, and temporary storage
- Network bandwidth
- Local IPC bandwidth
- Shared-memory bandwidth
- Required accelerator or device features
- Exclusive resources
- Startup and wake peaks

Every reported value must include a basis:

- `MEASURED`
- `ESTIMATED`
- `DECLARED_LIMIT`
- `UNKNOWN`
- `NOT_APPLICABLE`

Resource values may be left unknown during early development. The Manager may admit a provider with unknown values, monitor actual usage, and apply conservative limits. Measured history maintained by the Manager must remain separate from provider-declared values.

## 7. Manager communication

Every Manager command must contain:

- Command identifier
- Provider instance identifier
- Deadline
- Correlation identifier
- Requested action
- Required authority or permission context

Commands must be idempotent. Repeating the same command identifier must not repeat an already completed side effect.

Every running provider must send heartbeats containing:

- Provider instance and boot identifiers
- Residency and transition state
- Readiness
- Health
- Active request identifiers
- Current resource use when available
- Held control-authority leases
- Fabric connection state
- Last successful output timestamp

If Manager connectivity is lost, a provider must stop accepting new physical-control work and follow its declared disconnection behavior.

## 8. Fabric communication

Providers publish observations and metadata to the World State Fabric. Providers must not directly modify canonical world state.

Each observation must include:

- Schema identifier and version
- Provider instance and boot identifiers
- Stream identifier
- Sequence number
- Observation timestamp and clock domain
- Coordinate frame when applicable
- Calibration revision when applicable
- Confidence, validity, and expiration
- Related Skill or request identifier when applicable
- Inline payload or shared-memory reference

The Fabric decides whether an observation is current, valid, and accepted into canonical state.

## 9. Large-data rule

Images, video frames, depth maps, point clouds, audio blocks, and large tensors should use shared memory when producer and consumer are on the same machine.

The large payload itself must not be sent through the Fabric. The Fabric distributes a disposable reference describing how an authorized consumer can access the buffered payload. Consumers must reacquire a fresh reference when a ring-buffer slot has been recycled.

A shared-memory reference must include:

- Pool identifier
- Slot identifier
- Buffer generation
- Offset and byte length
- Format, shape, and stride
- Frame or sample identifier
- Observation timestamp
- Sensor or producer identifier
- Calibration revision when applicable
- Lease expiry
- Integrity state

Consumers must validate generation and lease validity before reading. A slow consumer must not indefinitely block the producer.

## 10. Physical-control authority

Any provider that can command a physical moving part must require a Manager-issued control-authority lease.

The lease may be assigned to:

- A Resource Provider
- A running Skill
- A safety controller
- An operator-control session

Physical commands should normally pass through the hardware Resource Provider even when a Skill owns the logical authority.

A control-authority lease must include:

- Lease identifier
- Controlled resource identifier
- Owner
- Permissions
- Issue and expiry times
- Renewal interval
- Fencing generation
- Preemption policy
- Safe relinquish behavior

When the lease is released, revoked, expired, or lost through disconnection, the hardware provider must enter its declared safe relinquish state while the controller and required hardware remain operational.

Examples include controlled deceleration, position hold, brake engagement, gravity compensation, safe gripper-force hold, or transfer to a designated safety controller.

Disabling torque is not an acceptable default for an arm that may fall or drop an object.

## 11. Failure format

Normal transport failures should use standard transport status codes. Provider-specific details must include:

- Stable namespaced error code
- Severity
- Related command, request, or Skill
- Affected functions
- Retry recommendation
- Safety impact
- Whether the physical outcome is known
- Structured diagnostics

Retry recommendations are:

- `DO_NOT_RETRY`
- `RETRY`
- `RETRY_WITH_BACKOFF`
- `RESTART_PROVIDER`
- `USE_FALLBACK`
- `REQUIRE_OPERATOR`

If a provider crashes or becomes unresponsive, the Manager creates a synthetic failure record using process exit data, last heartbeat, held leases, active requests, and available diagnostics.

## 12. Fallback

Fallback is selected by the Manager, not by the failed provider.

Fallback selection must consider:

- Interface and schema compatibility
- Safety level
- Coordinate and calibration compatibility
- Current residency and activation latency
- Available compute and device resources
- Acceptable degradation for the current Skill
- Conflicting physical-control authority

Fallback examples:

- GPU perception to a slower CPU provider
- Full-resolution perception to a lower-rate provider
- Desktop visualization to browser visualization
- Visualization to a headless recorder
- Local semantic model to a smaller local model
- Physical control to a verified safety controller, hold, or controlled stop

Simulation must never silently replace real perception or real physical control.

## 13. GUI and visualization providers

A GUI or visualization service is a normal Resource Provider.

It must declare:

- Interactive-session requirements
- Main-thread GUI requirements
- Display backend
- Headless support
- User-input support
- Graphics-memory estimate

Visualization is read-only by default. User actions must be sent through an authorized command path. A GUI crash must not stop robot safety supervision or physical control.

## 14. Versioning and conformance

Protocol versions must be negotiated during registration. Breaking semantic changes require a new major version.

A provider is conformant only after it passes the required tests in the Resource Provider Conformance Test Suite.
