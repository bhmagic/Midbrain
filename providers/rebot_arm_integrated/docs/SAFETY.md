# Safety — MIT bring-up 0.8.1

- Arm motion requires GUI Engage and Xbox LB. A new gripper endpoint requires GUI Engage and RB/RT; releasing RB/RT latches the endpoint.
- Agentic transit never uses the GUI/Xbox gate. It requires a separate exact
  controller preview plus a signed, short-lived, decision-specific, one-time
  authorization assertion. Approval alone does not execute; commit is a
  distinct request.
- Gamepad and Fabric target editing remain non-physical while Basic is in gravity-float.
- Free-space direct and routed targets accept displacements up to the 1.2 m request envelope; actual reach is determined by joint ranges, IK continuity, residual, singularity, and collision checks. CONTACT_WORK retains its separate 20 cm short-stroke policy.
- Workspace bounds are enforced before IK.
- Integrated physically exposes MIT, latched POS_VEL transit, baseline-budgeted latched arm POS_TOR work, and isolated joint-7 gripper MIT/POS_TOR tests. Basic remains final authority for Kp/Kd, torque, rate, tracking-effort, joint-limit, lease, and timeout validation.
- Manager discovery advertises usable MIT ONE_SHOT/HOLD_LB and POS_VEL ONE_SHOT. The old `pos_vel.one_shot_limited` name remains a deprecated compatibility alias. POS_VEL HOLD_LB and arm POS_TOR ONE_SHOT are experimental/unstable GUI-only tests and are excluded from `capability_readiness`.
- Requested and effective gains are exposed. Gain clamping is not hidden.
- Command gaps, lease loss, Manager/Fabric readiness loss, motion inhibit, unrecoverable serial/mode-confirmation failure, IK rejection, or continuous-replan faults request gravity-float.
- Signed transit revalidates the current semantic-scene revision, collision
  result, measured start drift, controller identity/configuration, preview
  expiry, and fenced Basic lease before its first physical endpoint.
- Signed transit derives requested per-joint speed from the Cartesian request.
  Any joint above 10 rad/s requires explicit authentication and any joint at
  or above 20 rad/s is rejected. Execution uses the smaller of the controller
  POS_SPEED cap, Basic's reported cap, and motor VMAX. It waits for stable measured arrival before
  advancing to the next exact controller-owned waypoint.
- A healthy signed transit holds its final POS_VEL endpoint until explicit
  `/v1/motion/path-release`. Errors and invalidated safety gates request
  gravity-float; normal short command gaps do not create an implicit mode
  switch.
- PRESS_MIT and TRANSIT_SPEED ONE_SHOT completion return to gravity-float. CONTACT_WORK returns to gravity-float when its configured one-shot duration expires, regardless of endpoint arrival. HOLD_LB release returns to gravity-float.
- CONTACT_WORK is ONE_SHOT-only and requires 6-DoF, a maximum 20 cm baseline-relative stroke, and a complete JOINT_6, WRENCH_6, or ISOTROPIC_2 budget. Baseline capture is a separate operator command performed in verified float; a later Engage + LB action uses that stored posture-local baseline. IK residuals are reported but do not reject the action. Required ratios and live residual overruns saturate affected joints at the reviewed physical POS_TOR ceilings instead of ending the action early.
- Releasing RB/RT latches the last gripper MIT or POS_TOR endpoint. LT, Float, and Safe terminate explicitly release the gripper latch.
- Payload mass/COM must be set only when known. Basic clips combined gravity feed-forward to configured motor TMAX.
- GUI Safe Terminate launches the authoritative PowerShell helper in a hidden
  process and waits for a launch-ID acknowledgement in
  `runtime_logs/safe_terminate.log`. Callers must treat only
  `status=accepted` with `safe_termination.state=RUNNING` as evidence that
  safe-home started. `LAUNCH_UNCONFIRMED` is a failure to start, not successful
  homing; use the standalone `stop_physical_gui_test.ps1` sequence and inspect
  the log.
