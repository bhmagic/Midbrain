# Phase 3 Gate 0 Physical Validation Report

Date: 2026-07-27
Status: completed; final safe-home and full shutdown confirmed

## Authorized scope

The user authorized one guarded lease-validity loop from safe-home with at most
`(0, 0, +0.02 m)` Cartesian motion. Thin rubber padding was present and did
not obstruct safe-home. No control-mode experiment, contact action, cutting
action, or additional gripper motion was performed.

The sequence was:

1. start Basic without submitting motion;
2. confirm healthy lease-free gravity-float;
3. run safe-home;
4. start Integrated and stage a nonphysical `+0.02 m Z` plan;
5. execute one reviewed `PRESS_MIT` one-shot;
6. exercise HOT to WARM to HOT to WARM lease ownership;
7. run final Basic safe-home while preserving the gripper;
8. stop Integrated, Basic, Manager, and Fabric.

## Confirmed behavior

- Initial Basic state was `SAFE_HOLD_GRAVITY_FLOAT`, healthy, lease-free, with
  fresh seven-motor feedback and no pending mode transition.
- Initial safe-home returned `already at safe-home`.
- Safe-home selected `PRESERVE_MEASURED_ANGLE` and retained the measured
  gripper target near `-0.362593 rad`.
- Integrated started HOT and disengaged, with a matching fenced Basic lease.
- The direct motion plan remained `SHADOW_NONPHYSICAL` until Engage plus the
  explicit LB rising edge.
- The `POSITION_3DOF` preview was valid with an approximately
  `0.0000611 m` position residual.
- The physical one-shot completed without a fault or sudden drop. Basic
  returned to gravity-float and all seven active modes were `IMPEDANCE`.
- Clean HOT to WARM release left Integrated healthy and WARM, Basic lease-free,
  and no background reacquire after a `1.8 s` observation interval.
- Explicit HOT recovery acquired a new lease at fencing generation 3. Basic
  reported the same lease ID and generation.
- A second WARM release remained stable for `1.4 s`, with both providers
  lease-free and no pending transition.
- Final safe-home completed from the raised posture. The measured gripper angle
  changed by exactly `0 rad` across safe-home.
- Integrated and Basic stopped gracefully. Manager and Fabric were then
  stopped by verified recorded PIDs. Ports 8791, 8793, 7001, and 7002 were
  closed.

## New controller finding

The reviewed target was exactly `+0.020 m` in Z with a `3.0 s` one-shot
duration. The measured controlled frame rose only about `0.0119 m` at its
maximum and ended about `0.0117 m` above the starting pose before the
controller floated.

The completion record correctly exposed:

- `arrival_joint_confirmed: false`;
- `arrival_cartesian_confirmed: false`;
- `arrival_duration_confirmed: false`;
- `float_confirmed: true`.

This is safe fail-soft behavior, but it is not successful target completion.
Fixed-duration `PRESS_MIT` one-shot currently floats at the duration boundary
even when arrival is unconfirmed. A caller must not equate `last_completed`
with target arrival. Phase 3 motion-policy work must either:

- make arrival a required success condition and report a distinct incomplete
  outcome; or
- use a controller-owned bounded settle extension before floating.

The controller must never extend motion without a hard maximum duration and
must still float on fault, authority loss, or deadline expiration.

## Startup time-control finding

Within the Codex-hosted PowerShell environment, invoking
`run_workspace.ps1` through a nested `powershell.exe -File` process can leave
the invoking terminal waiting after Manager and Fabric have already become
healthy and their PID file has been written. Duplicate case-variant `Path` and
`PATH` entries were one startup defect, but normalizing them did not eliminate
the nested-host wait.

Directly starting each reviewed executable through
`System.Diagnostics.ProcessStartInfo` was bounded and returned immediately.
It was used for the successful physical validation. The nested launcher is not
considered validated for agent use.

Until a dedicated launcher process has a software-only regression test:

- agent-run startup must use independent bounded operations;
- each health poll must have an internal deadline;
- the outer tool timeout must be only slightly longer than that deadline;
- a timed-out start must be followed by a bounded port/PID audit;
- no arm provider may be started until Manager and Fabric are independently
  confirmed healthy;
- partial core processes must be stopped by verified process name and PID.

## Gate decision

Phase 3 Gate 0 is closed. The lease serialization fix, safe-home gripper
preservation, residual rejection, and final shutdown all passed the guarded
loop.

The controller now reports the `PRESS_MIT` incomplete-arrival case as a
distinct non-success completion outcome without extending the physical
deadline. The Codex-hosted nested startup wait remains an open automation-host
issue with the documented bounded per-process workaround. Neither change
enables a broader physical execution policy.
