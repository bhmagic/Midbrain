# Phase 2 Physical-Control Safety Invariants

Status: mandatory during shadow evaluation

## Safety boundary

Phase 2 may observe Manager authority, compare it with provider-local state,
produce nonphysical plans, and record exact command submissions. It must not
use Manager authority to replace the Basic provider's local lease, switch a
motor control mode, engage physical motion, or submit a motor command.

The user-attended handover test is a separate stop gate. No lease handover,
control-mode switch, upstream-loss emulation, or motion beyond the separately
authorized 0--1 cm upward check is part of unattended Phase 2 work.

## Load-bearing owner

`robot_arm.rebot_dm` remains the independent load-bearing safety owner:

- it owns the hardware connection and the only hardware command lease;
- lease expiry, release, revocation, deadline expiry, and missing commands
  clear pending work and enter powered gravity-float;
- a mode transition freezes the previous endpoint before changing one motor
  at a time;
- every uncommanded load-bearing joint receives a measured-position,
  gravity-compensated hold;
- safe-home fences the active lease before its first supported frame, applies
  the safe-home gain floor, rate-limits the home target, verifies stable
  position and velocity, and returns to gravity-float;
- a control-loop exception attempts gravity-float before declaring a fault.

`robot_arm.primary.integrated` may hold a fenced Basic lease while HOT, but it
must verify gravity-float before engagement and before releasing that lease.
Platform loss, motion inhibit, stale Basic state, command gaps, trajectory
failure, and uncertain lease renewal all cancel motion and request float. If
the Integrated process disappears, the Basic lease expires and the Basic
provider independently enters gravity-float.

## Manager shadow-authority invariants

While `manager_authority.mode` is `SHADOW_OBSERVE`:

- `physical_enforcement` is always false;
- Manager authority polling may only read Manager state and update diagnostic
  comparison fields;
- polling may not acquire, renew, release, or replace the local Basic lease;
- polling may not engage the controller or change its control mode;
- polling may not stage, commit, or submit a motor command;
- Manager or Fabric unavailability must not disable Basic gravity support;
- a disagreement is diagnostic evidence, not permission to move.

These flags are exposed in the Integrated provider state and discovery
surfaces so a test can reject accidental enforcement before hardware use.

## Shutdown invariants

The Manager owns the global shutdown plan, while safety-critical providers own
their local safe termination:

1. stop the Integrated controller first so it cancels active trajectories and
   confirms Basic gravity-float before releasing its lease;
2. stop the Basic provider next through safe-home and powered settle;
3. stop noncritical providers;
4. stop Manager and Fabric only after safety-critical providers confirm;
5. never force-kill the Basic provider after a graceful-stop timeout;
6. if Manager is unavailable while an arm provider is reachable, refuse a
   workspace force-stop.

The existing `platform_core/scripts/stop_workspace.ps1` implements the
provider ordering and refuses to stop the core after an unconfirmed
safety-critical stop. The Basic provider registration keeps
`force_kill_on_stop_timeout` false.

## Command transparency

Every direct control request must be copied to the provider-local append-only
audit before the target operation runs. The audit contains:

- the exact canonical request and its SHA-256 hash;
- command, plan, skill, binding, and authority identifiers when present;
- SUBMITTED followed by ACCEPTED or REJECTED lifecycle events;
- the provider instance and boot identity;
- an asynchronous Fabric copy that never places Fabric in the synchronous
  motor-command path.

The local copy remains the source of truth when Fabric is slow or unavailable.
Pending Fabric copies are retried from the outbox.

## Physical-test gate

Before any permitted physical check:

1. inspect live provider and arm state;
2. confirm Manager registration, Fabric state, and motion inhibit;
3. safe-home first;
4. confirm gravity-float and no pending mode transition;
5. use a fresh fenced lease;
6. constrain the requested check to 0--1 cm upward;
7. return to gravity-float or safe-home;
8. stop immediately on stale state, lease uncertainty, command gap, or any
   unexpected direction.

The 10 cm setup motion and every handover/control-mode/upstream-loss test
require the user to be present with padding. Work must stop and call the user
before that gate.
