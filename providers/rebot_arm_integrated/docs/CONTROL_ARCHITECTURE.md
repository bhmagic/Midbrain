# Integrated arm control architecture

This document is the working design for the prototype. It separates planning from physical release so that path, force, and input behavior can be tested without treating a successful software test as permission to move the arm.

## Non-negotiable safety invariants

- A Fabric observation may stage a Cartesian target, tool transform, payload, execution mode, or semantic scene. It never grants physical motion authority.
- Direct control calls are recorded in the provider-local audit outbox before
  submission. Fabric receives a later shadow copy and is not a synchronous
  dependency of the control path.
- Motion requires a local operator action. The current physical path remains GUI Engage plus Xbox LB. Preview, mode selection, and scene updates do not imply engagement.
- Gravity-float is the immediate fallback. The already proven safe-home PowerShell path is the authoritative termination path.
- Low force, low torque, low Kp, or slow motion is not assumed safe. A low load-bearing Kp can let the arm fall under its own weight.
- Transport uncertainty, stale physical feedback, lost lease, Midbrain motion inhibit, or Basic FAULTED state blocks new commands and requests float where the lease is still valid.
- The controller does not automatically retry physical work after a fault or safety fallback. The operator must inspect state and authorize a new attempt.

## Authority coordination shadow

Integrated evaluates Manager task authority, operational-writer activity, and
the Basic residency lease through the versioned
`physical_agent.authority_coordination_state` schema. Manager and Basic
fencing generations live in distinct namespaces; equal numbers do not
establish lineage. Holding a Basic lease while HOT and idle is standby, not an
active upstream writer.

When an operational writer is active, a coordinated state requires its exact
upstream owner and Manager authority lease ID. The current execution boundary
does not yet supply those fields, so an active writer under simultaneous
Manager authority is intentionally reported as `DUAL_LAYER_UNCORRELATED` with
`AUTHORITY_LINEAGE_NOT_BOUND`. The state and disagreement counters are exposed
through `/health`, `/v1/state`, and `/v1/capabilities`. The evaluator is
strictly shadow-only: it cannot replace the Basic lease, change mode, or submit
motor commands.

## Execution modes

| Integrated mode | Basic backend | Command semantics | Intended use | Current release gate |
|---|---|---|---|---|
| `PRESS_MIT` + `ONE_SHOT` | `IMPEDANCE` | Stream time-indexed setpoints and float after completion | General reviewed one-shot motion | **USABLE; advertised** |
| `PRESS_MIT` + `HOLD_LB` | `IMPEDANCE` | Replan changed targets while LB remains held; release floats | Operator-supervised continuous target following | **USABLE; advertised** |
| `TRANSIT_SPEED` + `ONE_SHOT` | `POSITION_VELOCITY_LIMITED` | One endpoint plus bounded velocity limits; float after stable arrival | Ordinary free-space transit | **USABLE; advertised for IK-valid requests up to 1.2 m** |
| `TRANSIT_SPEED` + `HOLD_LB` | `POSITION_VELOCITY_LIMITED` | Replace only changed endpoints while held | Endpoint-overwrite research | **EXPERIMENTAL / UNSTABLE; not advertised** |
| `CONTACT_WORK` + `ONE_SHOT` | `POSITION_EFFORT_LIMITED` | One-shot endpoint using a separately captured posture baseline, physical-ceiling saturation, and timed float completion independent of arrival | 6-DoF force/torque research | **EXPERIMENTAL / UNSTABLE; not advertised** |

`GET /v1/capabilities` exposes the callable provider operation map. The provider heartbeat places only advertised capabilities in `details.capability_readiness`, which is the source consumed by the Manager's `GET /v1/capabilities`. The two experimental profiles remain accessible to the local hardware-test GUI but have no Manager-discoverable motion capability.

POS_VEL and POS_TOR must not receive a 100 Hz series of moving endpoints. Basic uses latest-envelope-wins ingress, and the motor protocol treats these modes as endpoint commands. Replacing endpoints before the motor settles can repeatedly restart its internal target behavior and can produce bounce or reversal. MIT is different because each frame is intentionally a cyclic spring setpoint.

TRANSIT_SPEED reads Basic's POS_SPEED caps (currently 5 rad/s for J1-J3 and 10 rad/s for J4-J6) and uses them consistently for duration and synchronized endpoint limits. Requested Cartesian speed is converted into per-joint demand; above 10 rad/s requires explicit authentication and at or above 20 rad/s is rejected. A rejected continuous replan does not revoke the last valid endpoint: Integrated retains and refreshes the last Basic-accepted endpoint and reports `HOLDING_LAST_VALID_POS_VEL_ENDPOINT`. This prevents an input-validation failure from causing an unrelated POS_VEL-to-float motor-mode transition.

Motor mode changes are staged one joint per Basic control tick. Before each potentially blocking register-10 confirmation, Basic refreshes the captured-position or gravity-supported hold for every other joint. Missing register-10 confirmation receives the same bounded serial retry as Windows transport timeouts. The POS_VEL endpoint remains withheld until all requested modes are confirmed. TRANSIT_SPEED ONE_SHOT requests float only after stable measured arrival. Gravity-float is not considered confirmed while Basic reports any pending motor-mode transition, and Integrated allows up to eight seconds for the staged transition to finish.

The gripper input begins with a joint-7-only request at a 10 Hz provider keepalive while Basic continues its 50 Hz MIT support loop. Basic also limits unchanged motor-side POS_VEL/POS_TOR endpoint frames to 10 Hz. RB selects the configured open endpoint at approximately -4.887 rad and RT selects close at approximately -0.349 rad. The GUI can select gripper MIT or gripper POS_TOR. Releasing the input latches that endpoint and keeps refreshing it. Each later arm command envelope includes joint 7 with the same latched endpoint, so Basic's latest-envelope rule cannot overwrite the gripper hold. LT, Float, or Safe terminate releases it. Starting a different gripper action is blocked while an arm trajectory is active.

## Planning and semantic objects

Every candidate path receives an immutable preview ID, target revision, preview
scene revision, joint start/goal, duration, sampled configurations, minimum
clearance, and collision list. A target, tool-offset, or settings change
invalidates the preview. A newer accepted compiler scene instead triggers a
whole-path collision recheck at commit; safe stored waypoints may continue and
the audit records both preview and commit scene revisions.

`POST /v1/motion/plan` combines target staging and preview in one direct
provider call. It remains `SHADOW_NONPHYSICAL`; singularity diagnostics and
speed/path-shaping policy stay controller-owned and are not interpreted as
Skill-level motion authority.

The scene input uses base-frame spheres with stable sphere and object IDs:

- `KEEP_OUT`: contact is never permitted.
- `PUSHABLE`: avoid first; contact requires an explicit per-preview policy.
- `WORK_OBJECT`: contact is permitted only when that object ID is explicitly named by the work request.

The controller and Fabric validator now implement the canonical scene-policy
boundary:

- Accept `WORKPIECE` as the contract term and normalize it to the controller's
  existing `WORK_OBJECT` collision-policy representation.
- Unknown objects default to `KEEP_OUT`; a task-selected manipulation target
  defaults to `WORKPIECE`.
- A VLM may emit a pushability hint, but only the upstream Agent or finite Skill
  may assert task-scoped `PUSHABLE` contact permission.
- `WORKPIECE` is contact-eligible at the declared acting frame by default.
  Explicit `NO_CONTACT`/standoff wording takes precedence, and non-acting link
  contact remains prohibited.
- Compile only geometry within 0.5 m of the measured gripper/controlled frame
  or 1.2 m of `rebot_arm_base`. Use minimum sphere radii of 20 mm in the gripper
  region and 60 mm elsewhere in the arm region. Inflate smaller geometry rather
  than deleting it, and voxelize/merge redundant samples before publication.
- The HOT `world_model.arm_scene_compiler` Provider is the single live owner.
  It applies ROI, minimum-size, semantic-default, and current robot-self-filter
  preconditions; merges Fabric-hosted point clouds and slower semantic
  assertions; and publishes monotonic, short-lived canonical revisions. Camera
  or perception Skills must not overwrite the controller scene independently.
  A metric `locate_item` result refreshes a short-lived `WORKPIECE` assertion.
  Empty/material-limited depth can degrade to semantic-only output only while
  an explicit assertion remains fresh; it never implies that unobserved space
  is clear.

Fabric validates contract version 2, base frame, canonical ROI layers, exact
minimum sphere sizes, explicit canonical type/scope, unique sphere IDs, and
in-ROI centers before accepting a scene envelope. This is a data-plane
contract, not collision-authority by itself; Integrated still revalidates the
fresh exact scene at authorized commit.

The optional preview diagnostic interpolates Cartesian position and orientation, solves each waypoint from the preceding joint solution, and samples the resulting joint segments against conservative link capsules. It reports low Jacobian singular values, large waypoint jumps, excessive endpoint joint displacement, and excessive aggregate joint travel. It does not block hardware execution. This is continuity analysis, not obstacle-route search.

Signed transit requests select a controller-owned final state. `FLOAT` returns
to verified gravity float, `FIXED` retains the endpoint until release, and
`WAIT_FOR_NEXT` retains it for a bounded consecutive-motion window. The latter
avoids a float/reacquire/mode-transition discontinuity between iterative
corrections while preserving fresh measured-start, authorization, and scene
revalidation for every chained path.

`WAIT_FOR_NEXT` currently accepts the successor after measured arrival. A
separate in-flight successor queue—where the next command arrives before the
current command finishes—is still TODO and must bind the successor to the
current plan's terminal state before permitting a seamless transition.

The physical DM serial loop and Integrated MIT stream default to 50 Hz for the next operator test. Reducing only Integrated would not reduce the actual motor traffic because Basic retransmits the current seven-joint command on every internal tick. Basic therefore exposes completed/attempted command frames, feedback requests and polls, mode switches, I/O error count, and measured rates.

## Torque baseline model

Baseline capture and CONTACT_WORK execution are separate operator actions. The capture command confirms gravity-float and records steady samples for 0.5 seconds. Capture is rejected if velocity or median absolute torque deviation exceeds configured per-joint limits. The later Engage + LB action uses the stored baseline, subject to the configured 20 cm baseline-relative posture check, and does not recapture automatically.

For joint `i`, the expected no-contact torque at the current pose is `tau_expected[i] = tau_baseline[i] + gravity_current[i] - gravity_baseline[i]`. The estimated external residual is `tau_external[i] = tau_measured[i] - tau_expected[i]`.

This subtraction compensates only for the change represented by Basic's calibrated arm-plus-payload gravity model. It does not make the result a Cartesian force measurement and does not remove friction, cable forces, sensor bias, acceleration torque, model error, or tool dynamics. Directional end-effector force reasoning therefore also needs the Jacobian and a reviewed uncertainty margin.

POS_TOR ratios are computed from expected absolute torque plus the operator-entered external torque budget and margin, divided by configured motor `TMAX`. The budget can be a direct six-joint vector, a controlled-frame six-axis wrench box, or isotropic force and torque magnitude balls. Wrench boxes map through `abs(J_controlled^T) * wrench_limits`; isotropic limits use the corresponding Jacobian row 2-norms multiplied by the force and torque magnitudes. A required ratio above the reviewed physical-test cap saturates at that cap rather than rejecting the task.

Integrated sends zero extra MIT feed-forward because Basic owns calibrated arm-plus-payload gravity torque. Height loss during rotation can therefore indicate an incorrect/missing payload model, gravity-model error, torque clamping, MIT tracking saturation, stale feedback, or serial loss. The state now reports Basic gravity torque, payload torque, clamp flags, hardware I/O counters, and controlled-frame height error so the next physical run can distinguish these cases.

If a CONTACT_WORK residual exceeds an operator-entered joint budget, the affected POS_TOR joint allowance rises to its reviewed physical ceiling and the same endpoint remains active until the configured one-shot duration expires. The event and joints are exposed as saturation telemetry. Timed completion then requests gravity-float; explicit Float/LT and authoritative Safe Terminate remain available earlier.

## Xbox mapping

- Sticks and D-pad edit the staged Cartesian target only.
- `Y` cycles `PRESS_MIT`, `TRANSIT_SPEED`, and `CONTACT_WORK` while settings are editable.
- `LB` is the current motion deadman/commit input.
- `RB` holds the gripper open; `RT` holds it closed using the backend selected in the GUI.
- `LT` requests immediate gravity-float and disengages.
- Clicking the left stick captures the steady torque baseline while verified in gravity-float.
- Holding `View + Menu` for two seconds launches the authoritative safe-home termination path.

The GUI provides the same controls and exposes the selected gripper backend, measured/target position, torque, and active action. Xbox convenience does not bypass engagement, lease fencing, or Midbrain inhibit.

## Next physical release sequence

1. Continue characterization of POS_VEL ONE_SHOT across the reachable workspace without payload or high external load.
2. Compare Basic hardware I/O telemetry and USB fault rate at 50 Hz against the earlier 60 Hz and 100 Hz observations.
3. Keep POS_VEL HOLD_LB in experimental GUI testing; do not publish it as an upstream capability until stable.
4. Keep CONTACT_WORK POS_TOR one-shot in experimental GUI testing; do not publish it as an upstream capability until baseline and force behavior are stable.
5. Physically characterize torque signs, bias, friction, gravity-model error, and transport-loss behavior before using CONTACT_WORK for precision tasks.
6. Physically qualify the HOT scene compiler and then add general waypoint/search planning without changing the authority rules above.
