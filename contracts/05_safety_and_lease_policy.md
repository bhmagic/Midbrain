# Safety and Lease Policy

Version: 0.3.10 Working Draft

## 1. Purpose

This policy defines software ownership of physical-control authority. It does not replace hardware safety functions, risk assessment, guarded workspaces, emergency stops, certified drives, brakes, or applicable machinery-safety requirements.

## 2. Control-authority rule

A Resource Provider must not command a protected moving part without a valid Manager-issued control-authority lease.

### 2.1 Phase 1 migration profile

The current implementation exposes Manager control-authority leases in
`ADVISORY` mode so their state can be compared with existing provider-local
fencing before enforcement. This mode does not satisfy the final
control-authority rule above and must not be interpreted as permission to
remove existing operator gates or provider-local leases.

Enforcement is enabled only after every relevant interconnection has been
observed, expiry and preemption behavior have been tested, and provider command
rejection is changed in a separate reviewed phase.

Protected resources may include:

- Robot arm motion
- Joint trajectory control
- Gripper motion or force
- Mobile base motion
- Camera pan and tilt
- Tool actuation
- Other mechanisms that can create physical movement or force

Resource IDs form a slash-delimited hierarchy. A lease for a parent conflicts
with every descendant. Sibling command groups may be leased concurrently only
when the hardware Provider declares disjoint actuator membership and enforces
commands at that group boundary. For example, `robot_arm.primary/arm` and
`robot_arm.primary/gripper` may coexist, while `robot_arm.primary` conflicts
with both.

The hardware Resource Provider is the final software enforcement point.

## 3. Eligible owners

A lease may be owned by:

- A Resource Provider
- A running Skill
- A designated safety controller
- An authenticated operator-control session

When a Skill owns authority, commands should normally still pass through the hardware Resource Provider.

## 4. Lease fields

Every control-authority lease must contain:

- Lease identifier
- Controlled resource identifier
- Owner identity
- Owner instance or Skill execution identifier
- Permission scope
- Issue time
- Expiry time
- Renewal interval
- Fencing generation
- Priority
- Preemption policy
- Safe relinquish behavior
- Disconnection behavior

## 5. Fencing

Every new authority grant must use the current fencing generation.

The hardware provider must reject commands containing an older generation, even when they come from a previously valid process.

Authority must not be transferred by handing the same lease to a new owner.

Transfer sequence:

1. Stop or fence the current owner.
2. Enter the required safe relinquish state.
3. Increment the fencing generation.
4. Issue a new lease.
5. Validate the new owner and generation.
6. Permit new commands.

### 5.1 Layered authority lineage during migration

The Manager authority generation and a hardware Provider's local operational
lease generation are separate fencing namespaces. Their numeric values must
not be compared, copied, or treated as proof that the two leases belong to the
same authority chain.

During shadow migration, an intermediary controller must report both views and
bind them with explicit lineage:

- Manager resource, owner, authority lease ID, and Manager fencing generation;
- local Provider lease ID and local fencing generation;
- the upstream owner and Manager authority lease ID under which the local
  lease was acquired;
- controller residency and local control state;
- motion-inhibit, authorization, and relinquishment state; and
- a versioned comparison state plus stable disagreement reasons.

Holding a Provider-local lease for HOT residency or gravity-supported standby
does not by itself mean that an operational writer is active. An active
operational writer is coordinated only when its upstream owner and Manager
authority lease ID exactly match the current Manager record. Numeric generation
equality alone has no meaning. Missing lineage for an active writer must be
reported as uncorrelated rather than inferred; an idle local lease must be
reported separately as standby, not as a conflict.

Shadow comparison is observation only. It must not replace the Provider-local
lease, switch a control mode, submit a motor command, or weaken an existing
fence. Poll, transition, state, and disagreement counts must be exposed before
any non-motion authority transition is considered for enforcement.

## 6. Safe relinquish

When a lease is released, expires, is revoked, or becomes unreachable, the hardware provider must perform the configured safe relinquish behavior.

The behavior is resource-specific and must be declared before use.

Possible behaviors include:

- Controlled deceleration to zero velocity
- Position hold
- Brake engagement
- Gravity compensation
- Safe bounded gripper-force hold
- Completion of a bounded stop trajectory
- Transfer to a designated safety controller
- Controlled hold while requesting operator assistance

The default must not be uncontrolled torque removal when that can cause a robot, payload, or tool to fall.

The provider may claim this behavior only while the required controller, drive, power, and hardware safety functions remain operational.

### 6.1 Process supervision during safe relinquish

Process termination is not automatically a safe state. If a Provider process is required to maintain gravity support, braking, payload retention, bounded gripper force, or another stable waiting state, its registration must disable automatic force-kill after a graceful-stop timeout.

For such a Provider, a missed graceful-stop deadline must produce a visible timeout or recovery-required state while the process remains alive. An authorized explicit force-stop remains available for emergency or operator-directed recovery.

A Provider may define a repeated explicit shutdown request as an
operator-directed recovery action. Its declared policy may permit process
release after fresh measured evidence confirms a stable non-moving state
without requiring one absolute position. If the Provider has already lost the
ability to control the resource, it may also permit process release while
marking the physical outcome unknown. Neither result restores operational
authority, and stale or missing evidence must not be represented as measured
stationarity.

Before an internal safe-home, controlled stop, or hold transition begins, the Provider must fence the outgoing operational authority, discard uncommitted commands from that owner, and reject late operational frames. The protective sequence must not compete with a still-valid task lease.

### 6.2 Global shutdown ownership

The Manager owns the intended global shutdown order. A compliant sequence
fences new authority, requests motion-provider safe relinquishment, confirms
the Basic controller's stable state, stops non-safety providers, stops
safety-critical providers only when doing so is confirmed safe, flushes
provider-local audit outboxes, and stops the Fabric. The workspace supervisor
stops the Manager last.

During Phase 1 the Manager publishes this sequence as `SHADOW_DRY_RUN` and does
not terminate processes. Existing proven safe-termination paths remain active
and must not wait for Manager or Fabric observation.

## 7. Lease expiry and renewal

Leases must expire automatically.

A lease owner must renew before expiry. Renewal requires:

- Valid owner identity
- Current fencing generation
- Healthy Manager connection
- Required safety conditions
- Provider readiness

The hardware provider must begin safe relinquish early enough to meet its declared stopping or hold requirement when renewal is missed.

## 8. Manager disconnect

On Manager disconnect:

- New physical-control work must be rejected.
- Existing authority must not be renewed indefinitely.
- The provider must follow its declared disconnected behavior.
- The provider must publish or locally record the authority-loss event when possible.

Reconnect does not restore old authority automatically. A new valid lease is required.

## 9. Skill completion and failure

A Skill must release temporary authority when it finishes, is cancelled, fails, or loses its execution context.

If the physical outcome is uncertain, the Skill and provider must report uncertainty. The Manager must not automatically retry an action that may already have occurred.

Examples:

- A gripper-close request timed out after the command was accepted.
- A placement request lost communication during release.
- Motion completion could not be confirmed after controller restart.

These cases require state verification or operator action before retry.

## 10. Priority and preemption

Suggested priority order:

1. Emergency and independent hardware safety
2. Designated safety controller
3. Operator safety intervention
4. Controlled abort or retreat Skill
5. Normal task Skill
6. Background calibration or maintenance
7. Visualization and diagnostics

Preemption must fence the old owner before the new owner can command.

A high-priority owner may request preemption, but the hardware provider must still perform the required bounded safe transition.

## 11. Stable waiting state

Every protected physical resource must define at least one stable waiting state.

The definition must state:

- Required power and controller conditions
- Whether position is held
- Whether brakes are engaged
- Whether gravity compensation is active
- Gripper behavior
- Maximum expected drift
- Maximum entry time
- Conditions that invalidate the state
- Required operator response when the state cannot be reached

For an arm holding an object, the waiting state must address both arm stability and gripper force.

## 12. Resource Provider residency

A provider in `WARM` must not retain physical-control authority unless it is a designated always-on safety controller and the Manager explicitly permits it.

A provider entering `WARM` must:

- Stop actuator output
- Release its lease
- Confirm safe relinquish
- Publish the resulting control state

A provider may remain `HOT` without holding control authority.

## 13. Emergency stop separation

Software lease loss, controlled stop, and Abort & Retreat are not emergency stop functions.

Emergency stop must remain independent and must not depend on the agent, Skill runtime, Manager, Fabric, network, or normal operating-system scheduling.

## 14. Minimum policy before real motion

Before enabling real robot motion, define and test:

- Protected resource list
- Stable waiting state for each resource
- Safe relinquish behavior
- Lease duration and renewal interval
- Fencing implementation
- Manager-disconnect behavior
- Provider-crash behavior
- Skill-cancellation behavior
- Preemption priority
- Outcome-uncertainty handling
- Hardware limitations, including power-loss behavior

Real motion must remain disabled until the physical-control conformance tests pass in simulation and a controlled hardware test environment.
