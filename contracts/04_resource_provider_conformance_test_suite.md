# Resource Provider Conformance Test Suite

Version: 0.3.9 Working Draft

## 1. Purpose

This suite verifies that a Resource Provider can safely and predictably operate under the Resource Provider Manager and World State Fabric.

The first version may be implemented as an executable test harness with mock Manager and Fabric endpoints.

## 2. Conformance levels

### Core

Required for every Resource Provider.

### Shared-memory

Required for providers that produce or consume large shared-memory payloads.

### Physical-control

Required for providers that command moving hardware.

### GUI

Required for GUI or visualization providers.

## 3. Core tests

### Registration

- Accepts a supported contract version
- Rejects or reports an unsupported version
- Uses a unique instance identifier
- Uses a new boot identifier after restart
- Does not report ready before registration completes

### Heartbeat and health

- Sends heartbeat at the negotiated interval
- Reports residency, readiness, health, and Fabric connectivity
- Reports a degraded state without falsely claiming full readiness
- Handles delayed heartbeat without duplicate state transitions

### Idempotence

- Repeated residency command with the same command identifier causes one transition
- Repeated request with the same command identifier does not duplicate side effects
- Repeated stop command remains safe

### Residency

- `COLD` to `HOT`
- `COLD` to `WARM`, when supported
- `HOT` to `WARM`
- `WARM` to `HOT`
- `HOT` to graceful stop
- `WARM` to graceful stop
- Readiness is false during incomplete warm-up

### Warm resource release

- Releases physical-control authority
- Stops actuator output
- Stops unnecessary high-rate processing
- Releases declared optional resources
- Reports intentionally retained resources

### Cancellation

- Cancels a cancellable request within its deadline
- Reports a non-cancellable phase explicitly
- Does not report success after cancellation unless the outcome is clearly identified

### Graceful and forced stop

- Exits before the graceful deadline under normal conditions
- Releases exclusive resources
- Leaves no orphan child process
- Manager automatically terminates the entire process tree after deadline failure when the registered policy enables escalation
- Manager preserves a timed-out process and reports recovery-required state when automatic escalation is disabled
- Explicit authorized force-stop terminates the complete process tree regardless of the automatic-timeout policy
- Safety-critical process preservation is tested without falsely reporting a successful stop

### Crash and restart

- Manager detects unexpected exit
- Manager creates a synthetic failure record
- Old instance and boot identifiers are rejected after restart
- Old leases cannot be reused

### Manager disconnect

- Stops accepting new physical-control work
- Follows declared disconnected behavior
- Does not continue renewing authority without the Manager
- Reconnects only with valid authentication and a valid instance policy

### Failure format

- Returns stable namespaced error code
- Includes retry recommendation
- Includes safety impact
- Distinguishes known from uncertain physical outcome

### Resource profile

- Accepts measured values
- Accepts estimated values
- Accepts explicit unknown values
- Does not interpret unknown as zero
- Reports actual usage when supported

## 4. Shared-memory tests

### Pool registration

- Registers valid pool metadata
- Rejects duplicate conflicting registration
- Rejects unauthorized consumer access

### Reference validation

- Valid reference is readable
- Wrong pool is rejected
- Wrong slot is rejected
- Wrong generation is rejected
- Out-of-bounds offset or length is rejected
- Expired lease is rejected
- Obsolete producer boot is rejected

### Commit ordering

- Consumer never observes a reference before payload commit
- Partial or invalid payload is not reported as complete

### Ring-buffer behavior

- Old non-leased slots can be reused
- Active read lease protects a slot until expiry
- Crashed consumer does not permanently block reuse
- Slow visualization consumer does not block capture indefinitely

### Queries

- Latest-frame query
- Frame-by-identifier query
- Nearest-timestamp query
- Recent-window query
- RGB/depth pair within tolerance
- No pair returned outside tolerance

### Producer crash

- New references stop immediately
- Streams become stale or unavailable
- Old references expire correctly
- Restart uses a new boot and pool generation domain

## 5. Physical-control tests

### Lease enforcement

- Command without lease is rejected
- Expired lease is rejected
- Wrong owner is rejected
- Old fencing generation is rejected
- Permission scope is enforced

### Lease release

- Explicit release triggers safe relinquish behavior
- Expiry triggers safe relinquish behavior
- Revocation triggers safe relinquish behavior
- Manager disconnect triggers declared safe behavior
- Skill completion releases or transfers authority through the Manager
- Provider-owned safe-home fences the outgoing operational lease before its first protective command
- Pending and late operational commands are rejected throughout safe-home or controlled stop

### Safe relinquish

The provider must demonstrate its declared behavior in simulation or a safe test mode:

- Controlled deceleration or stop
- Position hold, brake, or gravity compensation as declared
- Gripper behavior as declared
- No uncontrolled fall caused by ordinary software lease loss while required hardware remains operational
- State publication indicating authority loss

### Preemption

- Lower-priority owner is fenced
- New owner receives a new fencing generation
- Delayed old commands are rejected
- New commands are accepted only after safe transfer conditions are met

### Uncertain outcome

- Interrupted physical request reports outcome uncertainty when completion cannot be verified
- Manager does not silently retry a possibly completed physical action

## 6. GUI tests

- Runs with declared display backend
- Correctly reports interactive-session requirement
- Window close follows declared behavior
- GUI crash does not stop physical control or safety supervision
- Slow rendering does not block Fabric safety streams
- `HOT` to `WARM` releases high-rate subscriptions and rendering resources as declared
- Headless fallback can start when configured

## 7. Skill integration tests

A reference finite Skill should demonstrate:

- Requesting multiple Resource Providers
- Concurrent prewarming of independent providers
- Reading recent shared-memory frame references
- Cancellation
- Release of temporary leases
- Completion without stopping supporting providers
- Degraded result when an optional provider is unavailable

## 8. Pass criteria

A provider passes a conformance level only when all required tests for that level pass repeatedly under:

- Normal timing
- Delayed responses
- Duplicate commands
- Process restart
- Resource pressure appropriate to the provider

Safety-critical failures must block release of the provider for real hardware use.

## 9. Version 0.1 automation priority

Automate these first:

1. Registration and version negotiation
2. Heartbeat timeout
3. Residency transitions
4. Idempotent commands
5. Policy-aware graceful timeout and explicit forced process-tree stop
6. Shared-memory generation rejection
7. Read-lease expiry
8. Control-authority expiry and fencing
9. Safe relinquish in simulation
10. Crash and restart isolation
