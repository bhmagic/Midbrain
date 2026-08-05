# Supervised Vegetable Cutting Skill

This finite Midbrain Skill plans and executes a supervised vegetable-cutting
operation. It starts the required Providers, verifies a stationary world/arm
alignment, prompts the operator to load the knife and workpiece, performs one
initial VLM localization, converts the result to metric RGB-D geometry, and
installs a bounded hard-mount tool calibration before requiring explicit operator
takeover for motion.

This prototype is archived and excluded from normal Agent discovery. Its
general calibration, perception, registration, controller, audit, and
authorization responsibilities now belong to maintained components. Use Git
history for the former task plan and changelog; do not restore this package as
an active Skill.

After the human approves the first-cut approach, the Skill does not perform
coordinate or VLM checks between later cuts. Integrated preview and controller
health remain authoritative for every physical commit.

## Execution boundary

Version 0.3.0 stages Cartesian targets through Fabric and uses the Integrated
provider's operator-supervised settings, preview, Engage, LB, Float, and safe
termination operations. It has no gripper command. A generated plan is
non-physical until the configured hard-mount calibration is installed and the
operator explicitly authorizes takeover. The controlled point is 180 mm along
tool +X and 20 mm along tool -Z. The configured tool payload is 0.07 kg with a
zero COM offset. The initial VLM does not localize blade landmarks. The operator
reviews the physical blade location at the first-cut approach.

All arm segments use `PRESS_MIT` with `ONE_SHOT`. Targets are split into
commits no longer than 10 cm or 15 degrees, staged with freshness and expiry,
previewed before every commit, and advanced only after Integrated reports
trajectory completion with confirmed gravity-float. Kp remains a controller
profile multiplier, not a reliable cut-depth control. Cutting and post-cut
unsticking use the controller's maximum 10x multiplier. This raises the
configured wrist-joint Kp from 90 at 5x to 180 at 10x; the first three joints
were already capped at Kp 500 at 5x, and the change does not raise any motor
effort or torque limit.

## Workflow

1. Bootstrap camera, VIO, Basic arm, and Integrated Providers.
2. Verify all motion-control state is HEALTHY, fault-free, and trajectory-idle.
3. Verify a valid stationary alignment result, matching VIO epoch, matching
   camera-calibration revision, and current transform path. If alignment is
   missing or stale, start or reuse its GUI directly from the cutting GUI.
   The cutting GUI polls the published result and updates readiness without a
   restart. At cutting-session start, capture one RGB-D timestamp, resolve and
   lock the complete camera-to-arm pose, then stop only the local VIO Provider.
   This happens before waiting for tool or workpiece loading. Cutting requires
   the alignment result to contain the saved full
   `vio_from_camera_reference` pose. Legacy results without that field are
   rejected before motion; this Skill never substitutes a live VIO orientation
   because doing so can rotate camera corrections into the wrong arm-base axis.
4. Ask the operator to physically attach the knife, then record the human
   checkbox confirmation. Gripper-latch telemetry is not required.
5. Ask the operator to place the vegetable and leave the motion workspace.
6. Capture one RGB-D frame without VIO. Reuse the session-locked
   camera-to-arm pose and ask the VLM for the vegetable outline and two board
   points approximately 5 mm beyond its left and right ends. The later camera
   frame is bound to the alignment epoch through the immutable transform lock;
   it is not required to carry a live VIO epoch after the Provider is stopped.
   Capture the camera-to-arm and tool-to-arm transforms at the same timestamp
   before starting the slower VLM request. Scope the camera transform to the
   current VIO epoch, but leave the robot-local tool transform epoch-neutral.
7. Deproject those two pixels with aligned depth, transform them to arm-base
   3D, connect them with one straight line, and interpolate cut centers at the
   requested spacing. The cutting-board outline is not used in any calculation.
8. Install the configured hard-mount controlled-frame offset
   `[0.18, 0.0, -0.02]` m in the gripper/tool frame, with zero relative
   orientation, a 0.07 kg payload, and zero COM offset. VLM blade-tip
   registration is disabled for motion in this version.
9. Ask the operator to authorize takeover.
10. Move to the first approach with bounded, previewed MIT one-shots. First
    lift the controlled frame by 150 mm and preserve the measured gripper
    orientation. At clearance, ask the VLM only for the observed middle cutting
    point of the physical blade. Deterministic pixel/depth geometry compares it
    with a camera-space target that is fixed to the original RGB-D cut geometry,
    then replaces the unstable absolute camera-translation component before the
    X/Y transfer. The target overlay does not follow this correction. A
    person-or-animal alert from this multi-purpose observation is blocking only
    when a second focused presence-only VLM call confirms visible anatomy; the
    focused prompt explicitly excludes the robot, knife, cat tree, furniture,
    boxes, clothing, shadows, and reflections. Transfer X/Y at clearance and
    descend vertically to the review pose.
11. Ask the operator to review the physical first-cut approach. If the operator
    rejects it, run a bounded first-cut-only capture, deterministic relative
    translation correction, move, and recapture round. The VLM returns an
    observed blade midpoint rather than declaring the placement correct or
    estimating millimeters. Each round has bounded attempts and movement, but the
    operator may request another round without a session-wide limit. The review
    overlay uses orange for the board-plane cut and blue for the blade line at
    safe review height. These lines normally differ in the angled camera because
    of parallax; the VLM must not collapse the review clearance by moving the
    raised blade directly onto the orange image line. The VLM opinion about
    whether the orange line still crosses the vegetable is diagnostic only.
    The operator's physical first-cut review is authoritative. A correction is
    stopped if the recaptured pixel/depth residual does not improve by at least
    2 mm. Before any correction is applied, the Skill checks every corrected
    contact, approach, review, and 100 mm retract target against the workspace
    envelope published by Integrated. A correction that would make a later cut
    unreachable is rejected before the first cut.
12. After operator approval, run the remaining MIT cut, shift, and retract
    segments automatically without coordinate revalidation. Each cutting stroke
    uses 10x Kp. Each post-cut unstick stroke also uses 10x Kp and lifts 100 mm
    along the board normal before shifting laterally to the next cut.

The default first-cut VLM gate accepts confidence at or above 0.5 and a total
suggested translation up to 500 mm, but each physical correction remains
limited to 50 mm before mandatory RGB-D recapture. A deterministic board-normal
floor also prevents an automatic correction from reducing review clearance
below 60 mm. Rejected observations now report their reasons instead of being
summarized only as zero applied moves.
13. After all cuts, ask the operator to physically detach and remove the knife.
    The human confirmation, rather than gripper telemetry, authorizes Integrated
    safe termination. The Skill marks safe termination as started only after
    the detached helper acknowledges its launch and reports `RUNNING`. An
    unconfirmed launch remains at the tool-removal phase with an explicit error
    so the operator can retry or run Stop All; it is never reported as a
    completed homing action.

Run `scripts\setup.ps1`, then `scripts\run_gui.ps1`. Run
`scripts\check.ps1` for compilation and unit tests.

The runtime panel includes separate **Reload GUI** and **Reset failed session**
buttons. Reloading preserves the active session. Reset is enabled only in
`FAILED`, confirms that Integrated has no active trajectory, and clears only
the Skill's failed software state. It sends no motion, Float, or gripper command,
preserves the fixed-camera transform lock, and requires the operator to confirm
the still-attached tool again in the next session. Provider startup remains a
separate explicit control; merely opening the GUI does not request hardware.
Legacy automatic acquisition can be restored explicitly with
`MIDBRAIN_GUI_AUTO_BOOTSTRAP_PROVIDERS=true`. The localization readiness tile reports
`FIXED CAMERA LOCKED · VIO OFF BY DESIGN` after the session transform is locked.

## Mandatory test shutdown

Every live or Provider-backed test must finish by running the Midbrain
workspace shutdown:

`platform_core\scripts\stop_workspace.ps1`

Afterward, verify that the Manager, Fabric, camera, VIO, pose, Basic arm, and
Integrated arm endpoints are no longer listening. Stop any Skill GUI that was
launched separately. This cleanup is required even when a test fails, so camera
and robot hardware handles are released.

This release is an operator-supervised physical experiment, not evidence that
autonomous knife contact is production-safe. The stop control cancels the
Skill sequence and requests Integrated gravity-float. A hard stop remains
external.
