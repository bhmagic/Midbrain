# Arm Integrated Controller Feasibility and Handover

Version: design draft 0.1

## Decision

The Arm Integrated Controller is feasible as a one-to-one provider above each Arm Basic Controller.

For N arms, deploy:

- N Basic Controllers, each owning one physical motor bus.
- N Integrated Controllers, each owning the long-lived control lease for one Basic Controller.
- An optional later multi-arm coordinator for tasks that require coupled planning or inter-arm collision guarantees.

Recommended provider identities:

- `robot_arm.<arm_id>.basic`
- `robot_arm.<arm_id>.integrated`

Examples:

- `robot_arm.left.basic`
- `robot_arm.left.integrated`
- `robot_arm.right.basic`
- `robot_arm.right.integrated`

The current Basic Controller uses a fixed provider ID and will need an `arm_id`/`provider_id` configuration field before multi-arm deployment.

## Lease and residency behavior

The Integrated Controller acquires the Basic Controller lease when it becomes HOT and keeps renewing it while HOT, including while idle.

Idle behavior:

1. Integrated Controller keeps its Basic Controller lease.
2. Integrated Controller requests gravity-float.
3. Basic Controller stays in gravity-float while the lease remains active.
4. New integrated commands can start without a lease handover.

Termination or COLD transition:

1. Stop accepting new goals.
2. Cancel active planning and execution.
3. Request gravity-float from the Basic Controller.
4. Wait for a state acknowledgement.
5. Release the Basic Controller lease.
6. Basic Controller remains in gravity-float.

If the Integrated Controller crashes, lease expiry at the Basic Controller independently causes gravity-float.

## Recommended control modes

The proposed force/speed mappings should be renamed to avoid reversing their joint-level meaning.

### 1. `POSE_TRAJECTORY`

Purpose: collision-planned movement to a pose with Cartesian linear/angular speed and acceleration limits.

Input:

- Controlled frame.
- Goal pose.
- Maximum linear speed and acceleration.
- Maximum angular speed and acceleration.
- Optional target duration.

Execution:

- MoveIt generates a collision-free joint trajectory.
- Joint velocity limits are calculated along the trajectory.
- Basic Controller executes with `POSITION_VELOCITY_LIMITED` or buffered joint trajectories.

Feasibility: high.

### 2. `WRENCH_LIMITED_POSE`

Purpose: move toward a pose while limiting the joint torque budget corresponding to an allowed Cartesian wrench.

Input:

- Controlled frame.
- Goal pose.
- Six-component wrench limit: three forces in N and three torques in N·m.
- Speed limits.
- Contact policy.

Approximate joint torque budget:

`tau_limit(q) = abs(g(q, payload)) + abs(J(q)^T W_limit) + safety_margin`

The resulting per-joint limit is converted to the Basic Controller's `POSITION_EFFORT_LIMITED` torque ratios and updated as joint angles change.

This is a wrench-limited pose mode, not true Cartesian force tracking. FORCE_POS provides unsigned per-joint torque ceilings; it cannot enforce a direction-specific Cartesian wrench by itself.

Feasibility: medium-high for limiting; medium-low for accurate force control without a force/torque sensor.

### 3. `TWIST_SERVO`

Purpose: reactive Cartesian velocity control without requiring a fixed final goal.

Input:

- Controlled frame.
- Six-component Cartesian twist: three linear velocities and three angular velocities.
- Deadman/expiry deadline.

Joint velocity command:

`q_dot = J_damped_pseudoinverse(q) V + q_dot_null`

The controller scales the command for joint limits, singularity proximity, collision distance, and Basic Controller velocity caps.

Execution may use a short-horizon stream of joint position/velocity targets. MoveIt Servo is a suitable reference implementation because it supports Cartesian commands, joint-limit enforcement, singularity checking, collision checking, and smoothing.

Feasibility: high.

### 4. `HYBRID_AXIS_CONTROL`

Purpose: combine pose, velocity, force, and free/compliant behavior on different Cartesian axes.

A single six-number vector mixing force and speed is dimensionally ambiguous. Use a six-axis selection model instead.

For each axis X, Y, Z, roll, pitch, yaw, specify one of:

- `POSE`
- `VELOCITY`
- `WRENCH`
- `FREE`

The request then carries separate pose, twist, and wrench targets. Only the target selected for each axis is active.

This generalizes tasks such as:

- Move along a surface while maintaining normal force.
- Insert along Z while limiting lateral force.
- Hold orientation while allowing compliant translation.
- Push with an elbow while constraining other axes.

Feasibility: medium. It is the best long-term replacement for the proposed mixed three-force/three-speed mode.

### 5. `FLOAT`

Purpose: default idle state while the Integrated Controller retains the Basic Controller lease.

The Integrated Controller may provide a temporary payload model, but Basic Controller remains the actuator-level gravity-compensation authority.

## Dynamic speed and torque limits

### Cartesian speed to joint speed

For online motion, use a damped pseudoinverse and continuously rescale the result so no joint exceeds its velocity limit. Near singularities, reduce Cartesian speed or reject the request.

For planned goal motion, generate the path first, compute joint velocities along it, then apply trajectory time parameterization. This is more reliable than trying to convert one Cartesian speed number into seven independent fixed limits before a path exists.

### Cartesian wrench to joint torque budget

For acting frame `a`:

`tau_required = g(q, runtime_payload) + J_a(q)^T W_desired`

A conservative wrench ceiling can be converted to per-joint absolute limits:

`tau_limit_i = abs(g_i) + abs((J_a^T W_limit)_i) + margin_i`

Then:

`torque_ratio_i = clamp(tau_limit_i / configured_TMAX_i, minimum_ratio_i, provider_cap_i)`

This must be recomputed as angles, acting frame, tool, and payload change.

Friction is omitted in the first version. Actual contact wrench may therefore be less than requested, which is acceptable for a conservative first release. The API must report that the wrench is estimated/limited rather than measured.

## Temporary runtime payload

The Basic Controller requires a new in-memory runtime-load interface before the Integrated Controller can compensate for grasped tools and materials.

Suggested Basic Controller operation:

`SET_RUNTIME_PAYLOAD`

Fields:

- Payload ID and revision.
- Attached link or tool frame.
- Mass in kg.
- Center of mass XYZ in the attached frame.
- Optional inertia tensor.
- Source Integrated Controller lease/fencing generation.
- Update timestamp.

Rules:

- Runtime payload never overwrites the factory model or persistent calibration file.
- Restarting the Basic Controller resets the payload to the factory value.
- Updates are range-checked and ramped to avoid torque discontinuities.
- The active runtime payload is published to Fabric.
- Explicit grasp/release events should set or clear payload.
- A transient bump must not be interpreted as new payload mass.

Online payload adaptation should run only when the arm is quasi-static and a grasped-object state is active. Contact-wrench residuals during bumping belong to contact estimation, not gravity-model adaptation.

## Controlled frame and acting point

Every command must identify a `controlled_frame`. It is not limited to the flange or gripper tip.

Supported forms:

- Existing robot link frame.
- Tool center point attached to the gripper.
- Tool tip, blade edge, cutting point, or probe point.
- Virtual contact frame attached to any arm link.

For a grasped tool, publish:

- Tool geometry for collision checking.
- Tool-to-link transform.
- Active TCP or working-edge frame.
- Payload mass and center of mass.
- Permitted touch links.

For an elbow push, define a virtual frame on the elbow/forearm link and calculate that frame's Jacobian. Collision policy must allow contact only between the intended acting region and the designated workpiece. Other contacts remain prohibited.

Kinematic feasibility for arbitrary link-local frames is high. Reliable force control at those frames remains limited by torque sensing/model quality unless a force/torque sensor is available.

## Fabric sphere-scene format

A point set with a radius per point is feasible, but thousands of individual MoveIt CollisionObjects would be inefficient. The Fabric representation can remain a semantic sphere cloud, while the Integrated Controller converts it to an Octomap, voxel field, or grouped sphere geometry for planning.

Required scene metadata:

- Scene ID.
- Monotonic revision.
- Coordinate frame.
- Observation timestamp.
- Valid-until timestamp.
- Session epoch.
- Source provider.

Each sphere point should include:

- XYZ center.
- Radius.
- Confidence.
- Policy.
- Optional cluster/object ID.
- Optional material ID.
- Optional velocity estimate.

### Scene region and level of detail

The canonical arm scene should be clipped to the union of two base-frame
regions so point-cloud volume does not grow with the complete camera view:

- A high-detail sphere centered on the current measured gripper/controlled
  frame with a radius of 0.5 m.
- A complete arm-workspace sphere centered on `rebot_arm_base` with a radius of
  1.2 m.

The scene compiler should use a minimum collision-sphere radius of 0.005 m in
the 0.5 m gripper region and 0.015 m elsewhere in the 1.2 m arm region.
Observed geometry smaller than the applicable minimum is conservatively
inflated to the minimum; it is not discarded as free space. The compiler should
voxelize and merge redundant samples before publication. Geometry outside both
regions is excluded from the arm scene. A future mobile base can reuse the same
contract by moving the base-centered region with the current timestamped base
transform.

Policies:

### `KEEP_OUT`

Humans, walls, fixed environment, and other objects that must not be touched.

Behavior:

- Always treated as collision geometry.
- Inflation depends on confidence, velocity, and scene age.
- Human points should use larger margins and shorter validity periods.

### `PUSHABLE`

Objects that may be displaced.

Behavior:

- Avoid by default.
- A VLM may suggest that an object appears pushable, but that suggestion is
  context only and must not assign the canonical policy.
- Contact only when the upstream Agent or finite Skill explicitly enables
  pushing for the current task.
- Request should include maximum force, permitted displacement, and intended direction.
- If no pushing task is active, treat the object as `KEEP_OUT` unless it is the
  selected `WORKPIECE`.

### `WORKPIECE`

Material intentionally being manipulated, cut, pressed, inserted, or probed.

Behavior:

- Avoid during transit.
- The selected workpiece is contact-eligible by default at the declared acting
  frame. It does not need a second `PUSHABLE` classification merely because the
  gripper or tool may touch it.
- An explicit task-level `NO_CONTACT` or standoff requirement overrides the
  workpiece default. For example, a request to move close to a toilet-paper
  roll without touching marks the roll as the workpiece while retaining a
  no-contact clearance policy.
- Contact by non-acting robot links remains prohibited unless separately
  declared by the task policy.
- Apply task-specific wrench/speed bounds.
- Expected contact does not trigger a general emergency response.
- Unexpected contact outside the allowed region does.

Unknown objects default to `KEEP_OUT`. An object explicitly selected as the
manipulation target defaults to `WORKPIECE`. `PUSHABLE` is therefore a
task-scoped upstream assertion rather than the VLM's default classification.

Recommended representation is chunked by policy and cluster. Dense KEEP_OUT data should be voxelized or converted to an Octomap. Pushable and workpiece clusters should retain stable IDs and semantic metadata.

## Scene updates and replanning

The Integrated Controller maintains a thread-safe planning-scene snapshot and records the exact scene revision used for each plan.

Before execution:

- Confirm the scene revision is current enough.
- Confirm transforms and joint state are current.
- Recheck the trajectory against the latest scene.

During execution:

- Monitor newer scene revisions.
- Slow or stop if clearance becomes unsafe.
- Replan around new obstacles.
- Use a local reactive controller for short-horizon corrections.

### Controller-owned multistep routing TODO

The Integrated Controller should accept one goal, scene revision, and task
policy and produce a complete multileg route through the environment. The
route may contain clearance, lateral, retreat, observation, and final-approach
legs. The Agent should not need to issue or discuss every intermediate move.

The complete route, its alternative branches, and its permitted local replan
envelope remain controller-owned and are covered by one preview and one bounded
authorization decision. Integrated may select another reviewed branch or
insert local avoidance waypoints without a new Agent command only while the
goal, contact policy, speed/effort envelope, scene identity rules, and authority
scope remain unchanged. A changed goal or relaxed contact policy requires a new
decision.

The execution result should report every attempted leg, scene revision,
minimum clearance, replan cause, measured arrival, and terminal hold/release
state. This TODO depends on the canonical scene compiler and live scene-update
monitor but should precede removal of the current single-commit distance
restriction.

MoveIt Hybrid Planning is appropriate because it combines global and local planners running at different rates. MoveIt Servo is appropriate for local Cartesian velocity control and provides collision/singularity scaling.

If alternative paths fail, return a structured result rather than only a text error:

- Status: `BLOCKED`.
- Scene revision.
- Blocking cluster/object IDs.
- Closest clearance.
- Attempted planners and retries.
- Whether waiting for a scene update may resolve it.
- Suggested upstream choices, such as moving an obstacle, changing the acting frame, or relaxing a permitted-contact policy.

## Multi-arm behavior

One Integrated Controller per arm is the correct default deployment model.

Inter-arm collision handling requires one of:

1. Each arm publishes its current and predicted link geometry into the shared scene, and each Integrated Controller treats the other arm as a dynamic KEEP_OUT object.
2. A later multi-arm coordinator owns both Integrated Controller leases and plans against a combined robot model.

Option 1 is simpler but conservative. Option 2 is required for tightly coordinated bimanual motion.

MoveIt documents configuration for two or more manipulators, so a combined coordinator is feasible later.

## Danger policy boundary

“Danger” should be a policy plugin, not hardcoded into the motion planner.

The first release can expose policy inputs such as:

- Minimum link clearance.
- Human inflation margin.
- Maximum speed near KEEP_OUT points.
- Maximum allowed contact wrench by material/policy.
- Joint and workspace restricted zones.
- Stop/replan thresholds.

The planner produces a valid path under the active policy revision. The execution monitor independently enforces stop thresholds.

## Feasibility summary

| Capability | Feasibility | Main limitation |
|---|---|---|
| Pose goal with Cartesian speed limits | High | Requires reliable model, planning scene, and trajectory execution adapter |
| Cartesian twist servo | High | Singularity and latency handling |
| Wrench-limited pose through FORCE_POS | Medium-high | Limit only; not true wrench tracking |
| Accurate Cartesian force control | Medium-low initially | Needs better torque estimation or wrist force/torque sensor |
| Per-axis hybrid motion/force mode | Medium | Controller complexity and sensing quality |
| Temporary payload compensation | High | Requires Basic Controller runtime-load API |
| Tool-tip/working-edge control | High | Requires attached geometry, transform, and payload |
| Elbow or arbitrary-link acting point | High kinematically | Contact-force quality remains model-dependent |
| Semantic sphere-cloud obstacles | High | Requires downsampling/voxelization for dense clouds |
| Dynamic replan around obstacles | High | Needs scene freshness and execution monitoring |
| Two independent arms | High | Unique IDs, ports, namespaces, and scene publication |
| Coordinated bimanual motion | Medium | Needs later combined coordinator/planning model |

## Recommended implementation order

1. Parameterize Basic Controller with `arm_id` and provider identity.
2. Add temporary runtime payload API to Basic Controller.
3. Build one Integrated Controller in simulation with one Basic Controller.
4. Implement `FLOAT` and `POSE_TRAJECTORY` first.
5. Add sphere-scene ingestion and global replanning.
6. Add `TWIST_SERVO` with collision and singularity scaling.
7. Add tool/TCP and arbitrary-link acting frames.
8. Add `WRENCH_LIMITED_POSE` using dynamic gravity-plus-Jacobian torque limits.
9. Add `HYBRID_AXIS_CONTROL` after contact sensing and task policies are stable.
10. Add a multi-arm coordinator only after two independent Integrated Controllers are reliable.

## Primary references

- MoveIt Realtime Servo: https://moveit.picknik.ai/main/doc/examples/realtime_servo/realtime_servo_tutorial.html
- MoveIt Hybrid Planning: https://moveit.picknik.ai/main/doc/concepts/hybrid_planning/hybrid_planning.html
- MoveIt Planning Scene Monitor: https://moveit.picknik.ai/main/doc/concepts/planning_scene_monitor.html
- MoveIt Planning Scene ROS API and attached objects: https://moveit.picknik.ai/main/doc/examples/planning_scene_ros_api/planning_scene_ros_api_tutorial.html
- MoveIt dual-arm configuration: https://moveit.picknik.ai/main/doc/examples/dual_arms/dual_arms_tutorial.html
- MoveIt collision geometry and Octomap: https://moveit.picknik.ai/main/doc/concepts/kinematics.html
- ROS 2 PointCloud2: https://docs.ros.org/en/ros2_packages/humble/api/sensor_msgs/msg/PointCloud2.html
