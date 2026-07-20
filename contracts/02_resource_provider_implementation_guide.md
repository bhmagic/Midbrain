# Resource Provider Implementation Guide

Version: 0.2 Working Draft

This guide describes a preferred implementation pattern. It is not part of the wire contract unless a rule is explicitly repeated in the Resource Provider Contract.

## 1. Recommended language and runtime

Rust is recommended for the Resource Provider Manager, World State Fabric, and reference Resource Providers.

Recommended foundations:

- Stable Rust toolchain
- Tokio for asynchronous control-plane work
- Protocol Buffers and gRPC for typed control messages
- Dedicated worker threads for blocking vendor SDKs or predictable high-rate loops
- Small operating-system-specific modules for process groups, Windows Job Objects, shared memory, and GUI integration

Providers may use Python, C++, C#, Go, or another language if they implement the same contract.

## 2. Suggested provider structure

A provider should separate:

- Bootstrap and registration
- Manager control client or server
- Fabric client
- Residency controller
- Request dispatcher
- Resource reporting
- Health and heartbeat
- Shared-memory mapping or allocation
- Hardware or model adapter
- Diagnostics

Avoid placing vendor SDK calls directly inside control-message handlers. Dispatch them to a dedicated worker or adapter layer.

## 3. Startup sequence

Recommended startup order:

1. Parse bootstrap information.
2. Initialize minimal logging and diagnostics.
3. Connect to the Manager.
4. Negotiate the contract version.
5. Register the provider instance.
6. Connect to the Fabric.
7. Initialize only the resources required for the requested residency.
8. Run warm-up or self-tests when entering `HOT`.
9. Report readiness.
10. Begin regular heartbeat and resource reporting.

Do not advertise readiness merely because the process is running.

## 4. Residency implementation

### COLD

The process does not exist. The Manager owns this state.

### WARM

Recommended retained state:

- Configuration
- Minimal runtime
- Small checkpoints
- Small indexes or metadata
- Minimal Manager and Fabric connections

Recommended released state:

- Physical-control authority
- Actuator command loops
- High-rate subscriptions
- Large temporary buffers
- Most optional VRAM
- Large caches that are cheap to restore
- Exclusive device ownership unless explicitly approved

### HOT

A provider should be ready to accept requests without heavyweight initialization. Declared warm-up should already have run.

## 5. Fast activation

Use the following techniques where useful:

- Keep the process alive in `WARM`.
- Separate model loading from request execution.
- Preserve small checkpoints rather than large memory allocations.
- Pre-create shared-memory mappings.
- Pre-parse calibration and configuration.
- Use lightweight Fabric events to prewarm related providers.
- Activate independent providers concurrently.
- Keep a short rolling frame buffer so newly hot providers can inspect frames from immediately before a trigger.

Do not keep physical-control authority solely to improve wake latency.

## 6. Resource reporting

Early providers may use estimates or unknown values.

Recommended progression:

1. Start with `ESTIMATED` or `UNKNOWN` values.
2. Record actual use during startup, warm, hot-idle, and active work.
3. Add hardware-profile labels.
4. Update estimates after repeatable measurements exist.
5. Preserve observed variance instead of replacing it with one optimistic number.

Providers should report temporary startup and wake peaks separately from steady-state use.

## 7. Shared-memory implementation

Prefer fixed-size pools or ring buffers for camera and sensor streams.

A producer should:

1. Acquire an available slot.
2. Write the payload.
3. Finalize metadata.
4. Increment or assign the slot generation.
5. Commit the slot.
6. Publish the reference to the Fabric.

A consumer should:

1. Receive the reference from the Fabric.
2. Map or locate the pool.
3. Validate pool, slot, generation, bounds, format, and lease.
4. Acquire a short read lease if required.
5. Read without modifying the payload.
6. Release the read lease promptly.

Never trust an old pointer after the slot generation changes.

## 8. Physical-control implementation

A hardware provider should be the final enforcement point for physical-control authority.

Before accepting a command, validate:

- Lease identifier
- Owner identity
- Fencing generation
- Permission scope
- Expiry
- Command deadline
- Required robot state

On lease loss:

1. Reject new commands from the old owner.
2. Increment or adopt the new fencing generation.
3. Execute the declared safe relinquish behavior.
4. Publish control-loss state to the Fabric.
5. Wait for new authority or operator action.

Safety-critical hold or stop behavior should be implemented as close to the hardware controller as practical.

## 9. GUI and visualization providers

Many GUI frameworks require the main operating-system thread. A GUI provider may therefore use:

- Main thread for the GUI event loop
- Tokio or worker threads for Manager and Fabric communication
- Bounded channels between the GUI and background runtime

Closing a window should not automatically imply process kill. The manifest should define whether close means hide, enter `WARM`, or request graceful stop.

GUI rendering and subscriptions must not apply backpressure to physical control or safety streams.

## 10. Shutdown and crash behavior

Graceful shutdown should:

- Stop accepting work
- Cancel or drain active requests
- Release leases
- Stop high-rate publishing
- Flush minimal diagnostics
- Exit without orphaning child processes

The Manager should launch each provider inside an owned process tree:

- Windows: Job Object or equivalent
- Linux/macOS: dedicated process group or session

The Manager must reap children and kill the complete process tree after a missed deadline.

## 11. Logging and diagnostics

Each structured event should include when available:

- Provider identifier
- Instance identifier
- Boot identifier
- Correlation identifier
- Skill identifier
- Request identifier
- Lease identifier
- Timestamp

Secrets and sensitive sensor data should not appear in normal logs.

## 12. Reference providers to build first

Build these before generalizing the guide further:

1. Recorded RGB-D producer using a shared-memory ring buffer
2. Simple frame consumer
3. Lightweight compute provider with `WARM` and `HOT`
4. GUI visualization provider
5. Simulated arm-control provider with lease enforcement
6. One finite pointing-related Skill using multiple providers

Update this guide from verified implementation experience rather than adding speculative rules.
