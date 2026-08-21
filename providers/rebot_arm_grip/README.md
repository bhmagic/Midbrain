# Independent Gripper Control Provider

This Provider owns the gripper actuator-group lease and its persistent 50 Hz command stream. It accepts signed finite Skill commands, rejects a new grip when any active joint temperature is unavailable or at least 85 C, and proposes the configured wait time. Temperature does not block unrelated arm motion and never causes an automatic drop.

The owner-confirmed position convention is that more-negative joint angles open
the physical gripper. The effector profile distinguishes the fully open
`-280` degree endpoint from the functional `-180` degree open threshold used
for approach readiness. A signed `SET_MIT_POSITION` command interpolates from
the measured joint angle at 50 Hz for at least 1 second, extends the duration
when necessary to preserve the attended-development 4 rad/s cap, and then
maintains a bounded MIT position hold. `ready_for_approach` requires that hold,
measured functional openness, and fresh below-gate temperature feedback. The
4 rad/s owner-requested value exceeds the official reBot application 3 rad/s
setting and is not autonomous physical qualification.

The owner-observed normal-object firm-close endpoint is `+12` degrees. Basic separately enforces `+17` degrees as the absolute close limit. Under the owner-requested attended-development policy, stable contact is inferred only after absolute measured gripper-motor torque remains at or above `0.15` N m for 10 consecutive 50 Hz samples. Position and velocity remain published diagnostics but do not decide contact. Soft or thin object strategies are intentionally delegated to separately qualified Skills.

Finite mode transitions are serialized against the background 50 Hz command
stream. Basic therefore observes the complete command in the requested mode
before the Provider enables the matching mode guard. Repeating a `HOT` request
while the Provider is already HOT is idempotent and preserves its current
gripper-group lease.

Confirmed carrying is a cross-provider invariant. Contact owns joints 0 through 5 and the Grip Provider owns the gripper joint; Basic enforces `POSITION_EFFORT_LIMITED` on both groups. Confirmation succeeds only when Contact has bound the same carry identity and Basic reports every active joint in that mode. Idle remains position/torque locked while carrying.

Grip state publishes measured gripper position, velocity, and torque together with the current target and stable contact-sample count. The shared Skill runtime includes those bounded fields in failure details. Normal Grip Skills convert a missing-contact timeout into a verified open-and-float disposition plus an explicit unsuccessful task result.

That invariant is enforced while the confirmed-carry leases and control processes remain valid. Basic's existing emergency, hardware-fault, lease-expiry, and deadman fallback remains authoritative; the Grip Provider does not create indefinite unowned torque after a control-authority failure.

The release path first opens under `POSITION_EFFORT_LIMITED`. A separate signed command then requests a timed 50 Hz interpolation into MIT impedance and finally Basic's group float. MIT float is rejected while a carry is still bound.

The allowlist distinguishes `grip.grip`, the generic current-pose close, from
the compatibility identity `grip.grip_object`, whose user-facing label is
`Action: scrap grip`. The generic Skill cannot confirm carrying until Contact
has acquired and settled a measured-pose arm hold.

The Provider uses the existing generic Manager registration and Fabric observation APIs. It does not require Manager, Fabric, Agent, Mainframe, or Integrated changes.

Setup and software-only verification expectations are recorded in
[VALIDATION.md](VALIDATION.md). Release changes are recorded in
[CHANGELOG.md](CHANGELOG.md).
