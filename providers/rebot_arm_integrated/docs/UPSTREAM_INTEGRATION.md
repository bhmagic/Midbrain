# Upstream discovery and command integration

This is the integration reference for Skills, Agent hosts, external tools, and
coding agents. It describes discovery and target staging; it does not grant
physical authority.

## Terminology

- `rebot_arm_base` is the canonical Cartesian target and scene frame.
- The controlled frame is the frame whose pose Integrated solves. It may be
  offset from `rebot_arm_tool` by `ik_offset`.
- `ik_location` is the desired controlled-frame pose. A caller must not add or
  subtract `ik_offset`; Integrated applies that transform exactly once.
- `ik_gravity_offset` is a base-frame geometric correction to the staged IK
  location. It does not change Basic payload gravity torque.
- `TRANSIT_SPEED` uses Basic `POSITION_VELOCITY_LIMITED` (`POS_VEL`).
  `CONTACT_WORK` uses Basic `POSITION_EFFORT_LIMITED` (`FORCE_POS`), although
  Integrated telemetry and older documents commonly call it `POS_TOR`.

## Discovery

Integrated registers its control URL with Manager. Its heartbeat publishes
`details.capability_readiness`, which Manager exposes through
`GET /v1/capabilities`. The Provider-local `GET /v1/capabilities` maps current
capabilities and operator operations to their HTTP or Fabric invocation.

The current capability names and maturity are machine-readable in
`manifest.json` and the live capability response. The deprecated
`robot.motion.arm.integrated.pos_vel.one_shot_limited` name remains a
compatibility alias for `robot.motion.arm.integrated.pos_vel.one_shot`; new
callers should use the latter. Experimental profiles that are absent from
`capability_readiness` must not be inferred from GUI controls or changelog
entries.

Callable Provider operations include:

- `GET /v1/state` and `GET /v1/capabilities`;
- `POST /v1/settings`, `/v1/gripper/settings`, `/v1/scene`, and `/v1/preview`;
- `POST /v1/engage`, `/v1/teleop`, `/v1/gripper`, and `/v1/float` for the
  operator-supervised surface;
- `POST /v1/motion/plan` for direct nonphysical stage-and-preview;
- `POST /v1/motion/path-plan`, `/v1/motion/path-commit`, and
  `/v1/motion/path-release` for the signed transit boundary;
- `POST /v1/contact-baseline`; and
- `POST /v1/safe-terminate`.

Discovering an operation or Provider ID never authorizes it. State-changing
routes remain subject to the applicable operator or signed-commit boundary,
Basic lease fencing, motion inhibit, readiness, and Provider-side validation.

## Fabric Cartesian target

Default stream: `robot_arm.primary.integrated.command`

Schema: `physical_agent.arm_integrated_command`

The observation `data` object accepts:

- `command_type`: optional; currently `CARTESIAN_TARGET`;
- `ik_location.position_m`: preferred absolute `[x, y, z]` controlled-frame
  target in metres in `rebot_arm_base`;
- `ik_location.rpy_rad`: optional absolute `[roll, pitch, yaw]`, applied for
  `POSE_6DOF`;
- `ik_offset.xyz_m` and `ik_offset.rpy_rad`: optional
  tool-to-controlled-frame transform;
- `ik_gravity_offset.xyz_m` and `ik_gravity_offset.rpy_rad`: optional
  base-frame geometric correction; and
- `settings`: optional interaction mode, IK mode, duration, replan interval,
  gain multiplier, controlled-frame offset, payload mass/COM, or contact
  budget settings.

Legacy target and controlled-frame fields remain compatibility inputs where
the runtime schema declares them. New integrations should use the canonical
fields above and validate against the checked-in schema instead of copying a
field list from prose.

Fabric may update settings only while no trajectory is active. During active
motion, changed settings are rejected; target revisions may remain eligible
for the explicitly supported continuous profile.

## Freshness, identity, and acknowledgement

- Ignore observations with `valid: false`.
- Honor `expires_at_us` and the smaller of the Provider's configured maximum
  age and an observation-supplied freshness limit.
- Treat provider-instance, boot, and sequence as one identity tuple; never
  compare sequence numbers globally across producer boots.
- Use one command writer for a motion transaction. Concurrent writers make
  acknowledgement attribution ambiguous.
- Snapshot `fabric_input.accepted_count` before publishing. A matching
  sequence is accepted only when `last_result` is `ACCEPTED` or the accepted
  count increased.
- `DUPLICATE` or `STALE_IGNORED` does not prove that a new target was staged.

If acknowledgement is missing before freshness expires, publish the exact
same absolute target and settings with a refreshed observation lifetime and a
new sequence for the same producer instance and boot. Do not reuse a sequence
as a timestamp refresh. Stop immediately on a matching terminal `REJECTED`
result and surface `last_error`; retries must not hide a schema, workspace,
IK, or settings rejection.

Bound the total retry interval and report all attempted sequences, publication
count, final result/error, age, and before/after accepted count. After
acknowledgement, preview the exact staged target revision before entering the
applicable physical authority boundary.

## Recommended transaction

1. Resolve the Provider and inspect its current capabilities/readiness.
2. Snapshot acknowledgement counters and active-trajectory state.
3. Publish one bounded-lifetime target and settings observation.
4. Retry only with identical content and a new sequence when freshness
   requires it.
5. Confirm acknowledgement using sequence identity plus accepted count.
6. Request a nonphysical preview and verify its target revision and result.
7. Enter either the documented operator boundary or the separately signed
   path-commit boundary; never infer authority from Fabric acceptance.
8. Confirm physical start, terminal outcome, and requested final state before
   staging another action.

Every state-changing HTTP request is recorded first in the Provider-local
control-audit outbox and copied asynchronously to Fabric. Fabric availability
is not in the synchronous motor-control path. See
[Provider-local control audit](CONTROL_AUDIT.md).
