# Changelog

## 0.8.0 - 2026-07-29

- Added a versioned shadow evaluator that separates Manager task authority,
  Integrated operational-writer activity, and the Basic residency lease, with
  explicit upstream lineage, separate fencing namespaces, HOT-idle standby,
  stable disagreement reasons, and poll/state/transition/reason counters. It
  remains observation-only.
- Gripper `STOP` now clears the active action and target so the next
  controller-owned envelope omits joint 7 and Basic recaptures its measured
  angle while the arm endpoint remains held.
- Added adaptive Cartesian subdivision so controller-owned transit previews
  preserve sequential IK continuity within the configured adjacent-joint
  bound.
- Added separate whole-transit endpoint-delta and aggregate-joint-travel
  limits instead of applying the operator single-commit limit to every
  controller-owned route.
- Added signed, short-lived, exact-preview-bound, one-time physical transit
  commit and explicit gravity-float release endpoints.
- Revalidates controller identity/configuration, semantic-scene collision
  state, measured start drift, platform readiness, inhibit state, and the
  fenced Basic lease at commit.
- Executes only the exact controller-owned waypoint sequence, advances after
  stable measured arrival, caps every joint at 0.25 rad/s, and retains the
  final endpoint until an explicit release.
- Copies the exact control request and authorization assertion hash to the
  synchronous local audit while keeping both the raw assertion and Fabric out
  of the motor-command path.
- Completed a real OpenAI Agents SDK transit using the decision-specific
  commit boundary: 40/40 controller stages, 0.25 rad/s joint ceiling,
  0.05 m/s Cartesian ceiling, no reported fault, one accepted authorized
  transit, and zero rejected authorized transits.
- The controller retained the exact final authorized endpoint after completion
  and did not infer a release, gravity-float, controller-mode change, lease
  change, or safe-home command from agent completion.
- A later platform/authority-loss error moved that retained transit to
  `RELEASED` and latched Integrated `DEGRADED` while Basic remained connected,
  healthy, gravity-supported, and under a renewing local lease. No recovery or
  physical command was issued during this read-only observation.
- Unified the browser development controller GUI around the shared dark
  white/gray/black palette while retaining semantic warning and fault colors.
- During final shutdown, rejected unsafe/unreachable 20 cm, 19 cm, 15 cm, and
  14 cm upward targets through workspace, singularity, IK, joint-jump, and
  joint-travel gates. A 13 cm preview passed with no collision and 0.0514 m
  modeled clearance.
- The measured lift was approximately 12.67 cm. The eight-second one-shot
  retained its real `DEADLINE_FLOAT_BEFORE_ARRIVAL` classification with about
  7 mm Cartesian deadline residual and confirmed gravity-float before
  authoritative safe-home.
- Recorded Cartesian direction/frame interpretation as an open upstream
  contract. Controller-frame axes must not be inferred directly from words
  such as "up" without gravity and timestamped transform semantics.

## 0.7.0

- Publishes the configured Cartesian workspace in runtime state so upstream
  Skills can preflight a complete corrected path against the same envelope
  enforced by Integrated.
- Clamps the first outgoing trajectory command into Basic's existing
  operational range when a measured joint begins slightly outside that range;
  the Basic limits remain unchanged and recovery telemetry is published.
- Documents controlled-frame target semantics and reliable Fabric
  acknowledgement, retry, and terminal-rejection handling for upstream Skills.
- Launches Windows safe termination in a hidden process, waits up to five
  seconds for launch-ID acknowledgement, and reports unconfirmed startup
  instead of claiming that safe-home began.
- Publishes Manager `capability_readiness` and a provider-local `GET /v1/capabilities` operation map for upstream Skills.
- Marks PRESS_MIT ONE_SHOT and HOLD_LB usable; marks POS_VEL ONE_SHOT limited to paths at or below 20 cm without payload/high external load.
- Excludes experimental/unstable POS_VEL HOLD_LB and arm POS_TOR ONE_SHOT from Manager capability discovery while retaining them in the local hardware-test GUI.
- Makes CONTACT_WORK one-shot only, restores separate operator commands for posture-baseline capture and force execution, and removes continuous POS_TOR replanning.
- Removes hard IK position/orientation residual rejection. Residuals remain telemetry, and CONTACT_WORK ends on configured duration rather than endpoint arrival.
- Returns CONTACT_WORK to gravity-float at timed completion so the next one-shot does not remain blocked behind a latched trajectory.
- Saturates baseline-plus-budget ratios and live residual overruns at Basic's reviewed POS_TOR ceilings while retaining the active endpoint instead of faulting to gravity-float.
- Saturates POS_VEL speed requests at Basic's physical-test caps, automatically floats TRANSIT_SPEED ONE_SHOT after stable arrival, and replans HOLD_LB only when the staged target revision changes.
- Extends gravity-float completion verification to eight seconds for staged multi-joint mode transitions.
- Extends CONTACT_WORK to a 20 cm baseline-relative stroke and adds three selectable budget inputs: direct six-joint torque, a controlled-frame 3-force/3-torque box, and one isotropic force/torque magnitude pair valid in any controlled-frame direction. Cartesian budgets are conservatively mapped with the controlled-point geometric Jacobian transpose.
- Uses Basic's dedicated physical POS_TOR ceiling rather than the old general calibration ratios, while retaining configured motor TMAX as the hard ceiling.
- Makes the authoritative shutdown script continue to direct Basic safe-home when Integrated WARM cannot confirm float or release its lease. Process shutdown still stops if Basic safe-home is not confirmed.
- Physically enables configured CONTACT_WORK as a latched six-joint POS_TOR endpoint with 6-DoF IK, a fresh float torque baseline, explicit operator budgets, computed absolute ratios, and live residual monitoring.
- Corrects the observed gripper direction: RB/open now targets approximately -4.887 rad and RT/close targets approximately -0.349 rad.
- Adds explicit upstream `ik_location`, tool-to-acting-point `ik_offset`, and base-frame geometric `ik_gravity_offset` fields while retaining legacy Cartesian target fields.
- Moves unchanged POS_VEL/POS_TOR wire keepalive enforcement into Basic at 10 Hz and keeps changed endpoints immediate.
- Raises TRANSIT_SPEED's physical-test POS_VEL limits to the dedicated Basic caps: 2.0 rad/s for J1-J3 and 2.5 rad/s for J4-J6, while leaving MIT/calibration caps unchanged.
- Keeps the last Basic-accepted POS_VEL endpoint active when a later continuous replan receives HTTP 400 instead of changing motor mode or requesting gravity-float.
- Computes continuous TRANSIT_SPEED replans from fresh measured joints and applies the same provider POS_VEL caps to duration and synchronized endpoint-rate calculation.
- Replaced the earlier POS_VEL-to-MIT bridge with a direct POS_VEL-to-gravity-float completion after stable measured arrival; HOLD_LB remains available for changed-target endpoint testing.
- Changed GUI Safe Terminate from an unverified `LAUNCHED` result to a launch-ID acknowledgement with a deterministic Windows PowerShell path and a persistent runtime log.
- Reduced the physical Basic and Integrated loops from 60 Hz to 50 Hz after a repeated Windows serial semaphore timeout at 60 Hz.
- Added stable POS_VEL endpoint-arrival reporting without using arrival as authority to change motor mode.
- Kept state delivery and the testing GUI responsive during blocked serial I/O by using cached Basic telemetry and bounded, non-overlapping browser requests.
- Added direct `TRANSIT_SPEED` hardware execution using a persistent latched POS_VEL endpoint.
- Added ONE_SHOT and HOLD_LB continuous/receding-horizon MIT modes.
- Added adjustable duration, replan interval, and Kp multiplier.
- Increased bring-up translation projection to 20 cm.
- Added selectable 3-DoF position and 6-DoF pose IK.
- Added tool-relative controlled-point offset.
- Added payload mass/COM forwarding to Basic 0.1.20.
- Added effective per-joint gain/clamp telemetry and timing telemetry.
- Added Fabric Cartesian target consumer with freshness and duplicate filtering.
- Preserved gravity-float completion/release behavior.
- Reduced Basic physical I/O and Integrated MIT streaming from 100 Hz to 60 Hz and exposed Basic serial-I/O counters.
- Added joint-7 gripper testing with selectable MIT or POS_TOR, RB open, RT close, a latched last endpoint after input release, propagation of that latch into arm command envelopes, LT/Float explicit release, and a dedicated GUI panel.
- Staged POS_VEL/MIT register changes one joint per Basic tick with all other joint holds refreshed first; endpoint motion waits for full mode confirmation.
- Fixed the physical-test start script's undefined provider-root variable and made GUI safe termination a direct, deterministic detached launch.
