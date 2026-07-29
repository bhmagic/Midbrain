# Architecture — Integrated 0.8.0

Integrated is the motor-brand-neutral planning/control provider. Basic is the reBot/Damiao hardware adapter and hard-safety boundary.

1. Integrated leases Basic through the fenced operational lease.
2. Basic remains in powered gravity-float while the Cartesian controlled-frame target is edited.
3. Target input can come from the physical test GUI/gamepad or the configured Fabric command stream.
4. GUI Engage authorizes physical testing; Xbox LB commits arm motion, while held RB/RT controls the isolated gripper test.
5. ONE_SHOT uses fresh measured joints, projects translation to the configured envelope, and solves IK. PRESS_MIT streams at 50 Hz and floats after completion. TRANSIT_SPEED sends a latched POS_VEL endpoint and floats after stable arrival. CONTACT_WORK uses a separately captured posture-local float baseline, applies a baseline-budgeted POS_TOR endpoint for the configured duration, and floats when time expires without requiring endpoint arrival.
6. HOLD_LB replans PRESS_MIT and TRANSIT_SPEED only when the target revision changes. CONTACT_WORK is forced to ONE_SHOT. Releasing LB in either HOLD_LB backend floats immediately.
7. 3-DoF controls Cartesian position. 6-DoF also controls target orientation.
8. The controlled frame is `tool_frame × tool_to_control`, allowing a tool tip/acting point offset.
9. Integrated requests zero additional gravity torque. Basic computes arm plus payload gravity feed-forward using measured joints.
10. Basic's legacy MIT moving-target limiter remains for compatibility. Integrated rate-limits its waypoint progression so that legacy limiter should remain transparent rather than acting as a second trajectory generator.

Fabric is a target source in 0.8.0, not a motion-authority mechanism. It distinguishes absolute `ik_location`, tool-to-acting-point `ik_offset`, and base-frame geometric `ik_gravity_offset`. Operator debug motion still requires Engage + LB. Agentic transit instead requires a signed decision-specific assertion and the controller's exact current preview.
