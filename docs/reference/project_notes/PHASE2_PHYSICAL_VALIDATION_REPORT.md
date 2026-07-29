# Phase 2 Physical Validation Report

Date: 2026-07-27
Status: completed; authoritative safe-home and shutdown confirmed

## Scope and supervision

The physical work was performed with the user present. Padding was installed
for the elevated handover tests and removed before the final safe-home. Every
motion stayed inside the user-approved guarded envelope. No cutting or contact
operation was attempted.

The test began from safe-home, raised the controlled frame 10 cm, exercised
larger free-space translations and rotations, tested a small low-torque
gripper excursion, changed the arm between MIT and POS_VEL one motor at a time,
released and reacquired fenced leases, emulated loss of the Basic lease, then
returned through safe-home and stopped the workspace.

## Confirmed behavior

- The earlier safe-home/lease race fix prevented simultaneous Basic writers.
- A 30 cm upward target and a 30 cm forward round trip, executed through
  accepted intermediate waypoints, completed without a sudden drop.
- Roll targets near plus and minus 45 degrees completed at approximately
  plus 43.61 and minus 44.08 degrees.
- Unsafe or unreachable endpoints in the other directions were rejected
  instead of being sent.
- The gripper completed a small low-torque open/close excursion. Peak measured
  torque was approximately 0.193 to 0.208 N m, with no visible failure.
- All six arm motors changed into POS_VEL and back to MIT one motor per control
  tick. Twelve hardware mode transitions completed without a conflicting
  writer.
- A deliberate lease release did not cause a physical drop.
- Unexpected Basic lease revocation put Integrated into
  `RECOVERY_REQUIRED`; it did not reacquire in the background. Basic retained
  lease-free gravity-float until an explicit HOT recovery.
- The local direct-control audit remained complete: all observed records were
  published, with no pending or dropped record at the final inspection.
- Final Basic safe-home, Integrated stop, Basic stop, and workspace stop
  completed. Ports 8791, 8793, 7001, and 7002 were closed.

## Defects found

### IK residual was telemetry only

One downward shadow candidate was labeled valid even though its final
controlled-frame position residual was approximately 10.2 cm. Preview and
execution therefore need to reject residuals outside the configured IK
tolerances.

### Zero-length preview could look singular

A no-motion candidate could inherit `minimum_sigma = 0` and be described as a
singularity. Singularity travel checks are not meaningful when no Cartesian
motion is required.

### TRANSIT_SPEED arrival was too permissive

The POS_VEL trial declared completion after only three feedback frames while
the controlled frame remained about 6.8 mm short. Arrival must require joint
settling, controlled-frame residual, and completion of the controller-owned
duration.

### Deliberate WARM release raced lease renewal

The physical release was safe, but an in-flight renew request could observe
the intentional release as a lease failure and overwrite WARM with
`RECOVERY_REQUIRED`. Acquire, renew, release, and residency transitions need
one serialized lease-operation boundary.

### Gripper FORCE_POS velocity was not reliably bounded

A requested 0.10 rad/s produced a measured peak near 0.271 rad/s, and a
requested 0.04 rad/s produced a measured peak near 0.139 rad/s. MotorBridge
0.4.9 describes the FORCE_POS field as a rad/s velocity limit, but the hardware
observation shows that the native field alone is not a sufficient gripper
safety bound. The Basic provider needs its own joint-7 reference-rate limiter
while retaining the native velocity and torque ceilings.

### Safe-home changed the gripper target

Safe-home used the configured seven-joint home vector, which commanded the
gripper closed. Safe-home should home only the six arm joints and preserve the
installed gripper angle.

### Manager shutdown route was absent

The shadow Manager shutdown-plan request returned HTTP 404. The existing local
authoritative safety helper completed the shutdown correctly. Manager global
shutdown ownership therefore remains a Phase 3 interconnection task and must
not replace the local fallback until its route and acknowledgement protocol
pass.

## Phase 3 entry decision

Phase 3 development may begin, but enforcement is not enabled by this report.
The defects above form the Phase 3 closure gate. After offline regression, the
only initial physical recheck is safe-home followed by the newly authorized
`(0, 0, +0.02 m)` IK move for lease-validity testing. Any broader movement or
new control-mode experiment requires a new explicit scope.
