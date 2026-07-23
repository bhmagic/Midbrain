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

Runtime settings may be changed by Fabric while no trajectory is active. During active motion, a different runtime setting is rejected; target updates remain allowed for HOLD_LB replanning.

Freshness and duplicate handling:

- observations marked `valid: false` are ignored;
- the effective age limit is the smaller of provider `fabric_input.max_age_ms` and observation `freshness_ms` when supplied;
- `expires_at_us` is honored when supplied;
- duplicate provider-instance/boot/sequence tuples are not re-applied.

This release deliberately treats Fabric as a target source, not as physical authority. GUI Engage and Xbox LB remain required for physical motion.

The Manager advertises only the reviewed motion profiles:

- `robot.motion.arm.integrated.mit.one_shot`
- `robot.motion.arm.integrated.mit.continuous`
- `robot.motion.arm.integrated.pos_vel.one_shot_limited`

The POS_VEL one-shot capability is limited to paths at or below 20 cm with no payload or high external load. Continuous POS_VEL and arm POS_TOR one-shot remain experimental/unstable GUI tests and are not present in `heartbeat.details.capability_readiness`.

An upstream Skill can discover the provider through Manager `GET /v1/capabilities`, retrieve the provider `control_url`, inspect `GET /v1/capabilities` on the provider, and publish the Cartesian target/settings observation above. Operator Engage + LB remains the physical release boundary.
