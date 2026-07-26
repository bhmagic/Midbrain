# reBot Arm Integrated Motion Prototype

Integrated leases the Basic provider and exposes direct hardware test modes. Physical execution remains gated by GUI Engage plus Xbox LB.

Integrated owns its own Python environment at `providers/rebot_arm_integrated/.venv`, created by `scripts/setup.ps1`. It does not share Basic's environment, and neither environment is committed to Git.

The setup script also creates missing local `config/controller.json` from `config_templates/controller.default.json`. The active runtime config is machine-local and is not committed.

The reviewed discovery status is:

- `PRESS_MIT` with `ONE_SHOT`: **USABLE**.
- `PRESS_MIT` with `HOLD_LB`: **USABLE**.
- `TRANSIT_SPEED`/POS_VEL with `ONE_SHOT`: **LIMITED** to paths at or below 20 cm with no payload or high external load. Stability beyond those constraints is not established.
- `TRANSIT_SPEED`/POS_VEL with `HOLD_LB`: **EXPERIMENTAL / UNSTABLE**, GUI-only, and excluded from Manager capability discovery.
- `CONTACT_WORK` arm POS_TOR with `ONE_SHOT`: **EXPERIMENTAL / UNSTABLE**, GUI-only, and excluded from Manager capability discovery.

Two physical test interactions are available:

- `ONE_SHOT`: edit the staged target while floating, Engage, click LB once, and solve from fresh physical joints. PRESS_MIT streams and returns to float. TRANSIT_SPEED returns to float after stable measured arrival. CONTACT_WORK applies POS_TOR for the configured duration and then returns to float whether or not the IK goal was reached.
- `HOLD_LB`: while LB is held, PRESS_MIT or TRANSIT_SPEED replans only after the staged target revision changes; an unchanged target is not repeatedly resubmitted. Releasing LB floats. CONTACT_WORK is intentionally forced to `ONE_SHOT`.

## Operational-limit recovery at trajectory start

Basic rejects any commanded joint target outside its calibrated operational
range. A physical joint can nevertheless be measured slightly outside that
range after compliant contact or gravity-float. Integrated therefore clamps
the first outgoing trajectory target to the Basic operational range and then
continues inward toward the IK goal. It never expands or bypasses the Basic
limit. Inspect `trajectory.operational_range_recovery_count` and
`trajectory.last_operational_range_recovery_joint_indices` when diagnosing
this recovery. Skill authors should still avoid IK goals near a joint limit;
this recovery handles the otherwise unrecoverable first command, not a poor
task posture.

IK can be `POSITION_3DOF` or `POSE_6DOF`. The controlled frame can be offset from the tool frame, so upstream targets may represent a tool tip or other acting point. Cartesian targets are poses of that controlled frame; upstream callers must not pre-apply the configured tool-to-controlled offset. The GUI displays measured and staged frame axes plus separate position/orientation residuals.

The Kp field is a multiplier of Basic's nominal per-joint Kp profile. Requested and effective Kp/Kd are shown explicitly. Basic remains the final hard-safety authority.

Fabric input is enabled by default on `robot_arm.primary.integrated.command` using `physical_agent.arm_integrated_command`. It accepts explicit `ik_location`, tool-to-acting-point `ik_offset`, and base-frame `ik_gravity_offset` components while retaining the legacy target fields. Fabric updates only staged target/settings and does not bypass GUI Engage + Xbox LB.

The provider heartbeat publishes `details.capability_readiness`, which makes the reviewed capabilities visible through the Midbrain Manager at `GET /v1/capabilities`. The provider-local `GET /v1/capabilities` endpoint maps each advertised capability and GUI operation to its HTTP or Fabric invocation. Experimental continuous POS_VEL and arm POS_TOR one-shot execution are deliberately absent from the Manager capability map.

Payload mass and tool-frame COM are forwarded under the fenced Basic lease so Basic can add payload gravity feed-forward during motion and float.

Semantic sphere scenes and path preview remain available as optional diagnostics. They do not gate or authorize physical commands.

The GUI includes a dedicated joint-7 gripper test panel. Select `MIT` or `POS_TOR`, Engage physical control, then hold Xbox RB to open or RT to close. Releasing both latches the last selected endpoint and its keepalive so the gripper continues holding. Later arm envelopes include the latched joint-7 endpoint, preventing arm motion from overwriting the gripper command. LT, Float, or Safe terminate explicitly releases the gripper latch. Starting a new gripper action remains interlocked while an arm trajectory is active.

CONTACT_WORK requires `POSE_6DOF`, a baseline-relative stroke no longer than 20 cm, and one configured budget. Baseline capture and force execution are deliberately separate operator commands: first capture a posture-local floating baseline, then Engage and click LB for the one-shot action. The default direct J1–J6 external torque budget is 2, 2, 2, 1, 1, 1 Nm. IK position and orientation residuals are telemetry only and do not reject execution. The GUI also accepts a controlled-frame six-axis wrench box or one isotropic force/torque magnitude pair valid in any direction. Required POS_TOR ratios include expected baseline torque, the effective external budget, and three times measured baseline MAD. If that requirement or a live residual exceeds the budgeted allowance, affected joints saturate at Basic's reviewed 27 Nm J1–J3 or 7 Nm J4–J6 ceiling until the configured duration expires.

See `docs/CONTROL_ARCHITECTURE.md` for command overwrite semantics, torque-baseline math, safety invariants, Xbox mappings, and the physical release sequence.

See `docs/UPSTREAM_DISCOVERY.md` for capability names, maturity gates, and the upstream invocation map.
