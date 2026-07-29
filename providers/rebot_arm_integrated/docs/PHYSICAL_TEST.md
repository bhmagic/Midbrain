# Physical MIT bring-up test — 0.8.0

## Capability maturity

- PRESS_MIT ONE_SHOT: **USABLE**
- PRESS_MIT HOLD_LB: **USABLE**
- TRANSIT_SPEED/POS_VEL ONE_SHOT: **LIMITED** to paths ≤20 cm with no payload or high external load
- TRANSIT_SPEED/POS_VEL HOLD_LB: **EXPERIMENTAL / UNSTABLE**, GUI-only and not Manager-discoverable
- CONTACT_WORK arm POS_TOR ONE_SHOT: **EXPERIMENTAL / UNSTABLE**, GUI-only and not Manager-discoverable

Prerequisites: Midbrain Manager and Fabric healthy; Basic hardware provider healthy and in `SAFE_HOLD_GRAVITY_FLOAT`; Integrated healthy with its fenced Basic lease; no global motion inhibit.

Start with payload mass `0` unless the held tool mass and tool-frame COM are known.

Preview and semantic-scene input are optional diagnostics. Neither is required before the operator's Engage plus LB hardware command.

## ONE_SHOT first

Use `PRESS_MIT`, `POSITION_3DOF`, `ONE_SHOT`, duration `3.0 s`, Kp multiplier `1.0`, and an approximately 5 mm staged target change. Click Engage; the arm should remain floating. Click/release LB once. Confirm one smooth physical move, commanded joints differ from measured joints during motion, and completion returns to gravity-float.

Increase Kp gradually only after the 1.0x test is stable. Watch the effective-gain table. J1-J3 clamp at Kp 500, so requests above about 4.167x do not increase those joints further.

## HOLD_LB next

Use a small target and `0.10 s` replan interval. Hold LB and move the staged target slowly. Confirm the motor stream remains smooth across replans and releasing LB returns promptly to gravity-float. Experiment with replan interval only after the default behaves predictably.

## TRANSIT_SPEED hardware test

Select `TRANSIT_SPEED` and `ONE_SHOT`. Engage and click LB. Integrated sends and refreshes one latched POS_VEL endpoint. After the configured stable position/velocity window it automatically requests gravity-float. Requested joint speeds are saturated at Basic's physical-test POS_VEL caps rather than rejected.

`TRANSIT_SPEED` also supports `HOLD_LB` for the next continuous endpoint test. While LB is held, each new target revision may replace the latched POS_VEL endpoint at the configured replan interval; an unchanged target is not resubmitted as a new endpoint. Releasing LB explicitly requests gravity-float.

TRANSIT_SPEED uses dedicated physical-test caps of 2.0 rad/s on J1-J3 and 2.5 rad/s on J4-J6. If Basic rejects a later continuous endpoint with HTTP 400 after at least one endpoint was accepted, Integrated retains the last accepted endpoint and reports `HOLDING_LAST_VALID_POS_VEL_ENDPOINT`; it does not request float. A transport or Basic health fault is different because Integrated cannot assume that endpoint control remains available.

## Gripper hardware test

Use the dedicated panel to select `MIT` or `POS_TOR`. The physical mapping is RB/open at approximately -4.887 rad and RT/close at approximately -0.349 rad. Engage, then hold RB to open or RT to close. Releasing RB/RT stops changing the request and latches the last selected MIT or POS_TOR endpoint so the gripper continues holding. The UI buttons use the same press/release input. LT, Float, and Safe terminate are the explicit release paths. A latched gripper endpoint is carried in later arm command envelopes; starting a new gripper action remains blocked while an arm trajectory is active.

## CONTACT_WORK / POS_TOR test

Select `CONTACT_WORK`, `POSE_6DOF`, `ONE_SHOT`, and a target within 20 cm of the current pose. Choose JOINT_6, WRENCH_6, or ISOTROPIC_2 in the contact budget panel. The JOINT_6 default is 2, 2, 2, 1, 1, 1 Nm. ISOTROPIC_2 treats the entered force and torque as Euclidean magnitude limits valid in any controlled-frame direction, then maps their worst-case joint effects. With the arm in the posture to be used, click `Capture float torque baseline (manual)` and wait for `CAPTURED`. That command leaves physical control disengaged. Separately click Engage, stage the target, and click LB. Integrated uses the stored baseline and sends the POS_TOR endpoint without performing another real-time baseline capture. It applies the task for the configured one-shot duration and returns to float when time expires; reaching the IK goal is not required.

The GUI displays IK residuals as telemetry along with the selected budget mode, mapped joint budgets, calculated torque-limit ratios, live baseline-relative residual torque, and saturated joints. Neither position nor orientation residual rejects the physical action. A live torque residual beyond an effective joint budget raises affected joints to the physical ceiling while retaining the POS_TOR endpoint until normal timed completion.

## 6-DoF after 3-DoF

Select `POSE_6DOF` and make small orientation changes. Watch position and orientation residuals separately and compare target/measured coordinate axes.

GUI Safe Terminate is not considered physically verified by software tests. The authoritative shutdown command is:

`powershell -ExecutionPolicy Bypass -File "C:\Projects\testing_physical_ai_testing_pose\providers\rebot_arm_integrated\scripts\stop_physical_gui_test.ps1" -ProjectRoot "C:\Projects\testing_physical_ai_testing_pose" -StopCore`
