# Fabric Cartesian command contract

Default stream: `robot_arm.primary.integrated.command`

Schema: `physical_agent.arm_integrated_command`

The Fabric observation `data` object is:

- `command_type`: optional; currently only `CARTESIAN_TARGET` is accepted.
- `ik_location.position_m`: preferred absolute `[x, y, z]` target in metres in the reBot base frame. Legacy `target` and `cartesian_target` objects remain accepted.
- `ik_location.rpy_rad`: optional absolute `[roll, pitch, yaw]`; applied only in `POSE_6DOF`.
- `ik_offset.xyz_m` and `ik_offset.rpy_rad`: optional tool-to-controlled/acting-point transform. Legacy controlled-frame setting names remain accepted.
- `ik_gravity_offset.xyz_m` and `ik_gravity_offset.rpy_rad`: optional base-frame geometric correction added to the staged IK location. This does not change Basic payload gravity torque.
- `settings`: optional object containing `interaction_mode`, `ik_mode`, `duration_s`, `replan_interval_s`, `kp_multiplier`, controlled-frame offsets, payload mass/COM, or contact budget fields. Contact fields are `contact_torque_budget_nm` for JOINT_6; `contact_wrench_force_budget_n` plus `contact_wrench_torque_budget_nm` for WRENCH_6; or `contact_isotropic_force_budget_n` plus `contact_isotropic_torque_budget_nm` for ISOTROPIC_2. Select with `contact_budget_mode`.

`ik_location` is the desired pose of the configured controlled/acting-point
frame, not the raw robot tool frame. Integrated applies `ik_offset` (or the
equivalent controlled-frame settings) internally while solving IK. An upstream
skill must not add or subtract that tool offset from `ik_location`; doing so
applies the acting-point displacement twice.

Runtime settings may be changed by Fabric while no trajectory is active. During active motion, a different runtime setting is rejected; target updates remain allowed for HOLD_LB replanning.

Freshness and duplicate handling:

- observations marked `valid: false` are ignored;
- the effective age limit is the smaller of provider `fabric_input.max_age_ms` and observation `freshness_ms` when supplied;
- `expires_at_us` is honored when supplied;
- duplicate provider-instance/boot/sequence tuples are not re-applied.

Consumer acknowledgement:

- `fabric_input.last_result` describes the latest poll result, not a durable acknowledgement. A command can transition from `ACCEPTED` to `DUPLICATE` and then `STALE_IGNORED` while its target remains staged.
- Before publishing, snapshot `fabric_input.accepted_count`. Treat the command as accepted when the reported sequence matches and either `last_result` is `ACCEPTED` or `accepted_count` increased.
- Command identity is the provider-instance/boot/sequence tuple. A restarted producer can begin again at sequence 1 with a new boot ID; consumers must not compare sequence numbers globally across producers.
- A skill should use one command writer for a motion transaction. The compact provider status exposes the latest sequence and provider ID, so concurrent writers can make acknowledgement attribution ambiguous.

Reliable staging retry:

- Do not send Engage/LB or request preview until the Fabric command has been acknowledged.
- If the command is not acknowledged before its freshness window is nearly exhausted, publish the exact same absolute target and settings again with a refreshed `observed_at_us`, refreshed `expires_at_us`, and the next sequence number for the same provider instance and boot.
- Do not reuse the same sequence number as a timestamp-refresh mechanism. The provider-instance/boot/sequence tuple is duplicate identity, and a repeated tuple may continue to expose the earlier stale observation rather than provide a new staging opportunity.
- Keep the target and all runtime settings identical across staging retries. Incrementing the sequence creates another staging opportunity but does not authorize physical motion; physical execution still requires a successful preview followed by the operator-supervised Engage/LB boundary.
- Continue polling `fabric_input`. A retry is acknowledged only when `last_sequence` matches that retry and either `last_result` is `ACCEPTED` or `accepted_count` is greater than the pre-transaction snapshot.
- `STALE_IGNORED` with no `accepted_count` increase means that staging did not succeed. Do not preview the intended target, do not pulse LB, and do not interpret it as an IK or VLM rejection.
- Track every sequence published during one retry transaction. If
  `last_result` is `REJECTED` and `last_sequence` matches any sequence from
  that transaction, the target has received a terminal semantic rejection.
  Stop retrying immediately and surface `last_error`; do not hide a workspace,
  IK, or settings rejection by publishing the unchanged target under more
  sequence numbers.
- Bound the total staging retry time. On timeout, report the initial sequence, last published sequence, publication count, `last_result`, `last_error`, `last_age_ms`, and the before/after `accepted_count`.
- After acknowledgement, preview the currently staged target. Only a valid preview may proceed to the physical authority boundary.

Recommended transaction order:

1. Snapshot `accepted_count`, commit/reject counters, and active-trajectory state.
2. Publish the target with a fresh sequence and bounded lifetime.
3. Retry staging with newer sequences and identical content when necessary.
4. Confirm acknowledgement using sequence plus `accepted_count`.
5. Request nonphysical preview and validate the returned target revision.
6. Engage if required, then pulse LB exactly once.
7. Confirm commit start, completion, and gravity-float before staging the next target.

This release deliberately treats Fabric as a target source, not as physical authority. GUI Engage and Xbox LB remain required for physical motion.

The Manager advertises only the reviewed motion profiles:

- `robot.motion.arm.integrated.mit.one_shot`
- `robot.motion.arm.integrated.mit.continuous`
- `robot.motion.arm.integrated.pos_vel.one_shot_limited`

The POS_VEL one-shot capability is limited to paths at or below 20 cm with no payload or high external load. Continuous POS_VEL and arm POS_TOR one-shot remain experimental/unstable GUI tests and are not present in `heartbeat.details.capability_readiness`.

An upstream Skill can discover the provider through Manager `GET /v1/capabilities`, retrieve the provider `control_url`, inspect `GET /v1/capabilities` on the provider, and publish the Cartesian target/settings observation above. Operator Engage + LB remains the physical release boundary.
