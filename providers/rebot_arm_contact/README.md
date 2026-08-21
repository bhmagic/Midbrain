# Independent reBot Contact Work Provider

This Provider owns deliberate arm contact through Basic
`POSITION_EFFORT_LIMITED` commands. It is a separate process and Python
environment from the Integrated free-space controller and never calls or
imports Integrated.

Carry-bound v2 plans add only the persistence needed by gripping. After a
settled `PREPARE` plan is explicitly confirmed, Contact retains its existing
arm-group lease and guards all six arm joints in
`POSITION_EFFORT_LIMITED`. Matching `CONTINUE` plans replace the finite path
without changing that endpoint or lease boundary. Idle, plan gaps, and the
Contact watchdog hold the last position/effort target; WARM and controlled
stop are rejected until release. Gripper commands, object metadata, and
thermal admission remain outside Contact.

The Provider accepts an exact plan signed by an installed finite Contact Work
Skill. A move may be a direct one-shot endpoint or a Contact-owned Cartesian
segment. The segment is decomposed into sequential IK knots and streams
changing joint targets at the active Basic Provider's advertised internal
control rate (currently 50 Hz). Its default Cartesian command-speed ceiling is
0.1 m/s in addition to Basic's per-joint limits. A new move replaces the
preceding endpoint or in-progress segment while chaining from the previous
commanded joint setpoint instead of resetting to lagging measured joints. The
Provider establishes Basic's arm-group `POSITION_EFFORT_LIMITED` mode guard at
the first accepted endpoint and retains it across all Contact moves. It holds the final endpoint,
recalculates measured-pose wrench and gravity effort ceilings, and returns to
verified Basic gravity float only after `RELAX`, timeout, fault, lease loss, or
shutdown when no carry is confirmed. A confirmed carry converts ordinary
timeout/auth-expiry fallback into last-target holding; emergency and hardware
fault boundaries remain authoritative.

Contact acquires Basic feedback through an independent poller at that same
advertised rate. The command loop consumes the freshness-checked cache, so
feedback HTTP latency and command HTTP latency do not serialize every segment
update.

After plan acceptance and before sequence zero, no Contact motor endpoint is
active. A transient background-feedback failure in that interval marks the
Provider not ready but does not discard the signed session; sequence zero must
still obtain fresh Basic feedback before it can be accepted. Once any endpoint
is active, stale feedback remains a control fault and causes verified-float
cleanup. State retains `last_control_fault`, and an inactive-session rejection
includes that terminal cause and the most recent relax reason when available.
Cartesian segment construction also services the Basic lease between IK knots;
the control loop cannot renew concurrently while the move admission lock is
held.

The default inactivity timeout is six seconds after the accepted move's
Basic-limit-derived transition interval. Explicitly locked joints retain
their first captured session position with the full Basic-authorized effort
ceiling in N·m. Non-locked joints use the gravity-plus-wrench rule in
`contracts/21_contact_work_control.md`.

Cartesian targets are full 6-DoF poses. With or without locks, the independent
solver minimizes a weighted position-and-orientation residual, uses guarded
step acceptance, and returns the best pose it found. Locks remain hard even
when they make the exact pose unreachable; reported position and orientation
residuals expose that compromise without asserting task success.

Contact plans do not select joint speed. The Provider uses the effective
`POSITION_EFFORT_LIMITED` limits declared by Basic (`4.00 rad/s` for all six
arm joints in the current calibration). These are developmental arm-level
limits; the J4-J6 value exceeds the official reBot application `vlim` of 3.0
rad/s but remains below Basic's configured 10.0 rad/s motor envelope. Every move
result reports that frozen vector and a velocity-limited transition time from
fresh measured joints. A Cartesian segment time-parameterizes its sequential
IK intervals with that vector; a one-shot reports the direct joint lower bound.
The shared finite-Skill runtime waits for trajectory completion before starting
the signed physical-stage dwell and submitting a replacement; neither the
timing result nor the completed dwell is an arrival or stroke-success claim.

The default Cartesian knot spacing is at most 2 mm. Requested orientation is
held at every knot and hard joint locks remain exact. Joint interpolation
between knots makes this a close Cartesian approximation, not guaranteed
physical straight-line tracking or closed-loop force control.

A signed target may be an absolute Basic-root position or a Basic-root-axis
displacement from the fresh measured controlled-effector position at move
acceptance. The latter supports extraction whose direction must remain valid
even when a preceding deliberate-contact endpoint was not reached.

`GET /v1/contact/state` also exposes a read-only
`measured_acting_frame_pose` from the same independent FK and fresh six-joint
feedback. The generic `grip.grip` Skill uses its quaternion with a signed zero-
displacement `PREPARE` segment so arm ownership and all-joint position/effort
hold exist before the fingers close.

## Development setup

Run `scripts/setup.ps1`, then `scripts/register.ps1`. Setup creates or preserves
the allowlisted Contact Skill secrets, including Slicing and the generic,
scrap-grip, carrying-motion, and lay-flat identities, at least 32 bytes long in
the ignored API-key configuration. Provider and matching Skill hosts receive
the same per-Skill secret. Do not commit any of them.

The Manager reads `config/providers.json` and the manifest catalog once at
startup. Registration therefore affects the main menu only after restarting
Manager/Midbrain; the menu is not a live configuration reload.

Manager activates the Provider through `POST /v1/control/hot` before a Skill
starts. `WARM` retains the process and current joint-state publication but
rejects new contact sessions and holds no Basic control lease.

The active assembly must qualify
`robot_arm.motion.contact.position_effort_limited.v1`. The initial selection is
the blade development-v3 profile and is intentionally expected to change as
contact work is physically measured.

## API

- `GET /health` and `GET /v1/contact/state`
- `GET /v1/capabilities`
- `POST /v1/contact/session` with the signed complete plan
- `POST /v1/contact/move` with the session and next sequence number
- `POST /v1/contact/relax`
- `POST /v1/control/hot` and `POST /v1/control/warm`
- `POST /v1/safe-terminate`

Provider disposition reports command handling, not Cartesian arrival or task
success. The measured joint state is published as
`robot_arm.contact_work.state`.

## Provider documentation

- [Contact Skill authoring](docs/CONTACT_SKILL_AUTHORING.md)
- [Safety model](docs/SAFETY.md)
- [Validation](VALIDATION.md)
- [Changelog](CHANGELOG.md)

## Qualification

Version 0.2.0 is software-tested development code. It must not be represented
as physically qualified. Physical testing must retain load-bearing support and
must never introduce a low-torque or low-stiffness arm state.
