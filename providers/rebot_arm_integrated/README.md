# reBot Arm Integrated Motion Prototype

Integrated leases the Basic provider and exposes direct hardware test modes. Physical execution remains gated by GUI Engage plus Xbox LB.

If Basic fences or revokes the operational lease, Integrated enters
`RECOVERY_REQUIRED`, marks itself not ready, and stops background lease
acquisition. A later lease may be acquired only through an explicit HOT
transition after the Basic safety operation has completed.

Integrated owns its own Python environment at `providers/rebot_arm_integrated/.venv`, created by `scripts/setup.ps1`. It does not share Basic's environment, and neither environment is committed to Git.

The setup script also creates missing local `config/controller.json` from `config_templates/controller.default.json`. The active runtime config is machine-local and is not committed.

The reviewed discovery status is:

- `PRESS_MIT` with `ONE_SHOT`: **USABLE**.
- `PRESS_MIT` with `HOLD_LB`: **USABLE**.
- `TRANSIT_SPEED`/POS_VEL with `ONE_SHOT`: **LIMITED** to paths at or below 20 cm with no payload or high external load. Stability beyond those constraints is not established.
- `TRANSIT_SPEED`/POS_VEL with `HOLD_LB`: **EXPERIMENTAL / UNSTABLE**, GUI-only, and excluded from Manager capability discovery.
- `CONTACT_WORK` arm POS_TOR with `ONE_SHOT`: **EXPERIMENTAL / UNSTABLE**, GUI-only, and excluded from Manager capability discovery.

Two physical test interactions are available:

- `ONE_SHOT`: edit the staged target while floating, Engage, click LB once, and solve from fresh physical joints. PRESS_MIT streams until its fixed deadline and returns to float; the result separately reports whether stable measured arrival was confirmed. TRANSIT_SPEED returns to float after stable measured arrival. CONTACT_WORK applies POS_TOR for the configured duration and then returns to float whether or not the IK goal was reached.
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

At calibrated safe-home the default controlled frame is approximately aligned
with the arm base: controlled +X is arm-base/front and controlled +Z is
arm-base/up. Preserving that orientation during a full 20 cm +Z translation
requires approximately 0.81805 rad of joint-3 travel in the factory model.
The single-commit joint-3 endpoint guard is therefore 0.85 rad; the other
endpoint limits and all aggregate-travel, residual, singularity, workspace,
collision, authority, and physical-authorization gates remain unchanged.

Preview, fresh physical commit, and HOLD_LB replan reject position residuals
above the configured tolerance. `POSE_6DOF` also rejects excessive orientation
residual. Completion telemetry distinguishes a fixed deadline that floated
before stable arrival from a confirmed arrival; callers must inspect
`completion_success` and `completion_outcome` rather than treating a completed
deadline as target success.

The Kp field is a multiplier of Basic's nominal per-joint Kp profile. Requested and effective Kp/Kd are shown explicitly. Basic remains the final hard-safety authority.

Fabric input is enabled by default on `robot_arm.primary.integrated.command` using `physical_agent.arm_integrated_command`. It accepts explicit `ik_location`, tool-to-acting-point `ik_offset`, and base-frame `ik_gravity_offset` components while retaining the legacy target fields. Fabric updates only staged target/settings and does not bypass GUI Engage + Xbox LB.

The provider heartbeat publishes `details.capability_readiness`, which makes the reviewed capabilities visible through the Midbrain Manager at `GET /v1/capabilities`. The provider-local `GET /v1/capabilities` endpoint maps each advertised capability and GUI operation to its HTTP or Fabric invocation. Experimental continuous POS_VEL and arm POS_TOR one-shot execution are deliberately absent from the Manager capability map.

Payload mass and tool-frame COM are forwarded under the fenced Basic lease so Basic can add payload gravity feed-forward during motion and float.

Semantic sphere scenes remain optional for the operator-debug controls. They
are mandatory, revision-bound collision input for the signed agentic transit
path.

`POST /v1/motion/path-plan` also emits a short-lived, digest-bound nonphysical
preview contract for external authorization workflows. It binds the normalized
request, caller-supplied camera/workcell/VIO/scene context, selected plan,
controller instance/boot/configuration, and current lease snapshot. The
contract grants no commit authority and does not change control state.

`POST /v1/motion/path-commit` is a separate physical operation. It accepts
only the exact stored plan/request/preview digests and a
`X-Midbrain-Authorization` assertion signed by the configured authorization
identity. The assertion binds the controller provider, instance, boot,
configuration, plan, scene, decision, and expiry; both the assertion and plan
are consumed once. Commit rechecks the current scene, measured start drift,
collision clearance, whole-path joint travel, lease, inhibit, and controller
health before sending any endpoint. It then advances through the controller's
exact joint waypoints only after measured stable arrival, with both Cartesian
and per-joint speed caps. The final endpoint is intentionally held until
`POST /v1/motion/path-release` explicitly requests verified gravity-float.
Errors, lease loss, inhibit, stale platform state, or stage timeout request
gravity-float.

The authorization header is never copied into the audit. The synchronous
provider-local record contains its SHA-256 plus the exact submitted control
request; Fabric receives the asynchronous audit copy and is not in the motor
command path.

See `docs/CONTROLLER_PATH_PLANNING.md` for the complete preview/commit boundary
and physical execution invariants.

`POST /v1/motion/plan` now provides a direct, nonphysical controller-owned
stage-and-preview call. State-changing control submissions are copied first to
a provider-local append-only JSONL audit and then replayed asynchronously to
`robot_arm.integrated.control_audit`. Fabric availability is not in the
synchronous control path. A strict pre-action local-write failure blocks the
operation. A post-action audit failure is reported without rewriting a
successful hardware outcome as rejected. See `docs/CONTROL_AUDIT.md`.

The provider also observes the Manager's advisory authority view for
`robot_arm.primary` and reports how it compares with the existing fenced Basic
lease. The versioned comparison exposes both fencing namespaces, explicit
upstream lineage, Basic lease-held versus operational-writer state, controller
and relinquishment context, stable disagreement reasons, and
per-state/per-reason counters. Manager and Basic generation numbers are
deliberately never compared. A HOT idle Basic lease is reported as standby,
not as a writer conflict. Until an active writer receives and binds the exact
upstream Manager owner and authority lease ID, that active case is reported as
`DUAL_LAYER_UNCORRELATED`. This is shadow telemetry only: the provider-local
lease remains the enforced authority.

The GUI includes a dedicated joint-7 gripper test panel. Select `MIT` or `POS_TOR`, Engage physical control, then hold Xbox RB to open or RT to close. Releasing both latches the last selected endpoint and its keepalive so the gripper continues holding. Later arm envelopes include the latched joint-7 endpoint, preventing arm motion from overwriting the gripper command. LT, Float, or Safe terminate explicitly releases the gripper latch. Starting a new gripper action remains interlocked while an arm trajectory is active.

CONTACT_WORK requires `POSE_6DOF`, a baseline-relative stroke no longer than 20 cm, and one configured budget. Baseline capture and force execution are deliberately separate operator commands: first capture a posture-local floating baseline, then Engage and click LB for the one-shot action. The default direct J1–J6 external torque budget is 2, 2, 2, 1, 1, 1 Nm. Preview, commit, and replan reject excessive IK position or orientation residual before execution. The GUI also accepts a controlled-frame six-axis wrench box or one isotropic force/torque magnitude pair valid in any direction. Required POS_TOR ratios include expected baseline torque, the effective external budget, and three times measured baseline MAD. If that requirement or a live torque residual exceeds the budgeted allowance, affected joints saturate at Basic's reviewed 27 Nm J1–J3 or 7 Nm J4–J6 ceiling until the configured duration expires.

See `docs/CONTROL_ARCHITECTURE.md` for command overwrite semantics, torque-baseline math, safety invariants, Xbox mappings, and the physical release sequence.

See `docs/UPSTREAM_DISCOVERY.md` for capability names, maturity gates, and the upstream invocation map.
