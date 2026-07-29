# Phase 3 Gate 1 Manager Shutdown Validation Report

Date: 2026-07-27
Status: completed after one safety-critical classification fix

## Authorized physical scope

The user authorized a guarded raise of at most `+0.03 m Z`. The test used:

- Basic safe-home before the raise;
- Integrated `PRESS_MIT`, `POSITION_3DOF`, `ONE_SHOT`;
- no X/Y translation;
- no rotation;
- no gripper command;
- no control-mode or lease-handover experiment;
- Manager-owned safe-home and shutdown as the tested outcome.

All operations used bounded independent process startup, bounded HTTP calls,
and explicit health/state checks. The nested workspace launcher was not used.

## Raise result

The nonphysical preview requested exactly `(0, 0, +0.030 m)` from the measured
controlled frame:

- preview position residual: `0.0000424 m`;
- minimum Jacobian sigma: `0.05117`;
- collision-free: true;
- physical execution blockers: none.

The one authorized physical commit:

- raised a maximum and final `0.02067 m`;
- returned Basic to seven-joint `IMPEDANCE` gravity float;
- left no mode transition;
- preserved the gripper at approximately `-0.362593 rad`;
- reported `completion_success=false`;
- reported `completion_outcome=DEADLINE_FLOAT_BEFORE_ARRIVAL`;
- measured a `0.00935 m` Cartesian residual at the deadline;
- confirmed gravity float.

This validates the new outcome telemetry and confirms that fixed-duration
PRESS_MIT still does not guarantee endpoint arrival.

## Unsafe first plan caught before execution

The first Manager plan had zero blockers but was unsafe:

- step 2 contained both `robot_arm.primary.integrated` and
  `robot_arm.rebot_dm`;
- the Basic safe-state and final Basic-stop steps were empty.

Basic advertises some `robot.motion.*` capabilities. The planner classified
motion providers before checking the Basic safety-provider identity, so Basic
matched the broader motion rule first.

The execution route was not called. The arm remained in healthy gravity float.
The proven direct Basic safe-home then completed from the raised posture,
preserved the gripper exactly, and cleared the lease. Integrated, Basic,
Manager, and Fabric were stopped in the local authoritative order.

## Fix and regression

`build_shutdown_plan` now gives the Basic safety-provider classification
priority over the broader motion-provider classification. The regression
deliberately gives Basic both `robot_arm.gravity_float` and a
`robot.motion.*` capability and asserts:

- Integrated alone is in
  `REQUEST_MOTION_PROVIDERS_SAFE_RELINQUISH`;
- Basic alone is in `CONFIRM_BASIC_SAFE_STATE`;
- Basic alone is in `STOP_BASIC_PROVIDERS_AFTER_CONFIRMATION`.

Rust formatting, 18 platform tests, strict Clippy, and the release build passed.

## Corrected Manager execution

To avoid spending a second physical-motion authorization, the corrected
release was tested from an already confirmed safe-home:

1. Fabric and Manager started independently.
2. Basic started healthy, lease-free, and gravity-floating.
3. Direct safe-home confirmed `PRESERVE_MEASURED_ANGLE`.
4. Integrated started HOT, disengaged, and held a matching fenced Basic lease.
5. The corrected shutdown plan had zero blockers and the exact required
   provider classification.
6. Execution returned HTTP 202 and completed in approximately `0.64 s`.

Every step was confirmed:

- the Manager fenced new starts, HOT transitions, and authority acquisitions;
- Integrated stopped and relinquished its Basic lease;
- Basic safe-home returned `success=true`,
  `safe_state_confirmed=true`, and the preserved gripper target;
- ordinary stopped providers were acknowledged as already stopped;
- Basic stopped only after its safe-state acknowledgement;
- Fabric remained alive for supervisor inspection;
- Manager remained alive until the supervisor stopped it last;
- the execution had no failures and reached `AWAITING_SUPERVISOR`.

The supervisor verified ports 8791 and 8793 were closed, stopped Fabric by its
recorded PID, stopped Manager last by its recorded PID, and confirmed no
relevant listener remained.

## Policy decision

Manager-owned global shutdown is enabled in this development workspace and is
the default `stop_workspace.ps1` path when Manager reports shutdown execution
enabled. The local sequence remains available through
`-UseLocalShutdownFallback`.

New installations retain a disabled template default. Capability binding,
Manager control authority, strict controller audit, and agent physical
execution remain non-enforcing.
