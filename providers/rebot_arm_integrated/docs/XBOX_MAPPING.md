# Xbox mapping — Integrated 0.7.0

- Left stick left/right: base X target edit.
- Left stick up/down: base Y target edit.
- D-pad up/down: base Z target edit.
- 6-DoF only: right stick left/right: base-Z yaw.
- 6-DoF only: right stick up/down: base-Y pitch.
- 6-DoF only: B/X: positive/negative base-X roll.
- LB: rising-edge commit in ONE_SHOT; hold for continuous replanning in HOLD_LB; release HOLD_LB to gravity-float.
- RB: command gripper open using the GUI-selected MIT or POS_TOR backend; release latches that endpoint.
- RT: command gripper closed using the GUI-selected MIT or POS_TOR backend; release latches that endpoint.
- LT: immediate gravity-float and disengage.
- Y: cycle the arm execution mode while motion is idle.
- Left-stick click: capture the floating torque baseline.
- Hold View + Menu for two seconds: authoritative safe-home termination.

GUI Engage is required before LB arm execution or a new RB/RT gripper command. A latched gripper endpoint is included in later arm command envelopes. New gripper input remains interlocked while an arm trajectory is active.
