# Integrated Provider safety boundary

Integrated plans and coordinates motion, but the Basic Provider remains the
final hardware authority for motor mode, load-bearing stiffness and damping,
joint/rate/effort limits, tracking effort, lease fencing, deadlines, gravity
support, and safe-home. Read [Basic safety](../../rebot_arm_dm/docs/SAFETY.md)
before physical use.

Low force, low torque, low stiffness, or slow movement is not inherently safe.
In particular, lowering load-bearing MIT stiffness can allow the arm to fall
before gravity support stabilizes.

## Physical authority boundaries

Local operator testing requires GUI engagement plus the documented gamepad or
GUI action. Editing a target, changing a profile, receiving a Fabric command,
or generating a preview remains nonphysical.

Agentic transit uses a different boundary: an exact controller-owned preview
must be followed by a signed, short-lived, decision-specific, one-time commit.
The commit revalidates controller/configuration identity, the reviewed
workcell activation, observation lifetime, measured start, current semantic
scene and collisions, path limits, motion inhibit, readiness, and the fenced
Basic lease. Approval or preview alone does not execute motion.

The current capability names and maturity are authoritative in
`manifest.json` and the live capability response. A profile exposed only by
the local test GUI is not thereby available to an Agent.

## Fallback and completion

Transport uncertainty, stale physical feedback, lost lease, motion inhibit,
Manager/Fabric readiness loss where required, Basic fault, invalidated
workcell/scene evidence, or an execution error blocks new commands and
requests gravity float when the lease is still valid. Integrated does not
automatically retry physical work after a safety fallback.

Completion telemetry distinguishes elapsed command duration from stable
measured target arrival. Callers must inspect the reported outcome and final
state; they must not infer success from a completed deadline. Signed transit
may request `FLOAT`, `FIXED`, or bounded `WAIT_FOR_NEXT`, each enforced by the
stored preview and commit contract.

## Contact and gripper limits

`CONTACT_WORK` requires `POSE_6DOF`, a configured short-stroke limit, a
separately captured steady gravity-float baseline, a complete joint/wrench
budget, and an IK result inside the configured position and orientation
residual tolerances. Required effort ratios and live residual overruns may
saturate affected joints at Basic's reviewed physical ceilings; this is
telemetry, not proof of contact safety or Cartesian-force accuracy.

A released gripper input intentionally latches the last selected endpoint.
Float, LT, and safe termination explicitly release that latch. New gripper
input remains interlocked while an arm trajectory is active.

Payload mass and tool-frame center of mass must be supplied only when known.
Basic clips combined arm-plus-payload gravity feed-forward to configured motor
limits.

## Safe termination

The GUI safe-terminate route launches the authoritative PowerShell helper and
waits for a launch-ID acknowledgement. Only `status=accepted` together with
`safe_termination.state=RUNNING` is evidence that the helper started; neither
state proves that safe-home completed.

On an unconfirmed launch, use
`scripts/stop_physical_gui_test.ps1` and inspect
`runtime_logs/safe_terminate.log`. Do not force-kill a process that may be
providing powered support merely to free a port.
