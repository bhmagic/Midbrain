# Upstream discovery and invocation

The Integrated provider registers its control URL with the Midbrain Manager. Its heartbeat publishes `details.capability_readiness`, which the Manager exposes through `GET /v1/capabilities`.

## Advertised motion capabilities

| Capability | Maturity | Constraint |
|---|---|---|
| `robot.motion.arm.integrated.mit.one_shot` | USABLE | Operator Engage + LB remains required |
| `robot.motion.arm.integrated.mit.continuous` | USABLE | Operator Engage + held LB remains required |
| `robot.motion.arm.integrated.plan.direct.nonphysical` | SHADOW | Direct stage and preview; cannot authorize motion |
| `robot.motion.arm.integrated.pos_vel.one_shot` | USABLE | IK-valid free-space request up to 1.2 m; actual reach, scene, and joint limits apply |
| `robot.motion.arm.integrated.pos_vel.one_shot_limited` | DEPRECATED ALIAS | Compatibility alias for `robot.motion.arm.integrated.pos_vel.one_shot` |

`TRANSIT_SPEED`/POS_VEL `HOLD_LB` and arm `CONTACT_WORK`/POS_TOR `ONE_SHOT` are experimental and unstable. They remain available in the local test GUI but are deliberately omitted from Manager `capability_readiness`.

## Provider operation catalog

After resolving the provider `control_url`, call `GET /v1/capabilities`. The response maps the GUI operations to their HTTP routes and maps Cartesian target staging to the Fabric stream.

The callable HTTP operations are:

- `GET /v1/state`
- `POST /v1/engage`
- `POST /v1/teleop`
- `POST /v1/settings`
- `POST /v1/gripper/settings`
- `POST /v1/gripper`
- `POST /v1/preview`
- `POST /v1/motion/plan`
- `POST /v1/contact-baseline`
- `POST /v1/scene`
- `POST /v1/float`
- `POST /v1/safe-terminate`

Absolute Cartesian target and settings staging is also callable by publishing schema `physical_agent.arm_integrated_command` to Fabric stream `robot_arm.primary.integrated.command`.

The Engage and teleop routes are classified for an operator or an
operator-supervised Skill. Publishing a target, discovering a capability, or
calling a configuration route does not grant autonomous physical authority.
The Manager now exposes advisory control-authority leases, and Integrated
compares that view with its fenced Basic lease. Because the Manager lease is
not enforced in Phase 1, the local Engage + LB gate remains required.

Every state-changing HTTP route is copied to the provider-local control-audit
outbox. Fabric receives the same event asynchronously, so Fabric latency or an
outage does not delay the controller call.
