# Integrated Provider validation

Run the Provider's stopped check from the repository root:

```powershell
.\providers\rebot_arm_integrated\scripts\check.ps1
```

Use the current command result as the software evidence. Do not copy a test
count from a prior release into an installation decision.

The current checkout has known package-version drift across machine-readable
surfaces. Do not publish an Integrated release until those values agree; do not
choose a value from the changelog or a README title to silence the conflict.

## Stopped coverage

The regression suite covers capability publication, exclusion of
GUI-only experiments from Agent discovery, Basic lease transitions, IK and
residual gates, contact-budget mapping, gripper latching, payload forwarding,
Fabric freshness and duplicate handling, 50 Hz command pacing, interpolation
timing, Basic-calibrated rate limiting, already-satisfied-pose Jacobian
measurement, controller-owned preview, one-time signed commit, final-state handling,
semantic-scene revalidation, provider-local audit behavior, authorization
redaction, and Manager-versus-Basic authority shadow telemetry.

Stopped tests do not establish motor direction, stopping distance, payload
behavior, collision clearance, torque sensing accuracy, serial robustness, or
safe-home behavior on a particular installed arm.

## Guarded physical evidence

A guarded no-contact signed transit has completed through the exact preview,
authorization, and staged-waypoint boundary. Earlier attended testing also
exercised MIT motion, gravity-float recovery, Basic lease handover, bounded
POS_VEL transit, gripper control, and authoritative safe-home. These results
are evidence for the tested workcell and configuration only; they are not a
general deployment qualification.

## Remaining physical qualification

Before broad autonomous use, qualify at least:

- `TRANSIT_SPEED` across the intended reachable workspace, loads, speeds, and
  stopping conditions, including measured 50 Hz command cadence and tracking;
- Basic hardware I/O and USB fault behavior at the configured loop rates;
- payload mass/COM handling and gravity-model error;
- semantic-scene production and collision checking against measured geometry;
- authority-lineage binding from Manager decisions to the enforced Basic
  lease; and
- gripper stop/release behavior and safe termination under transport and
  process faults.

`CONTACT_WORK` remains outside qualified autonomous scope. Its promotion-or-
retirement work is tracked in the
[framework roadmap](../../docs/09_LIMITATIONS_AND_ROADMAP.md#qualify-or-retire-integrated-contact_work),
not as an implied future capability in this validation record.

The file-integrity manifest is authoritative for the current source payload.
Provider environments, active machine-local configuration, runtime audit logs,
caches, and generated files remain outside that payload.
