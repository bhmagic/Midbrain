# Hardware Development UI

The standalone Hardware Development UI is an attended local client for
measured-state inspection and bounded manual joint testing. It does not plan
autonomous trajectories, estimate calibration parameters, record calibration
sessions, or write the active calibration profile.

Start the Basic Provider first, then launch the UI with
`scripts/run_calibration.ps1`. The launcher name and `/v1/calibration/*`
manual-control routes are retained for compatibility. The Provider flag
`--allow-hardware-calibration` is likewise a compatibility name for enabling
physical attended-development leases; none of these names advertise an
automatic calibration workflow.

## Manual control boundary

The UI exposes the Provider's reviewed attended-test modes:

- `IMPEDANCE` with the Provider-enforced load-bearing `kp` floor;
- `POSITION_VELOCITY_LIMITED` with Provider-enforced position and velocity
  limits; and
- `POSITION_EFFORT_LIMITED` with Provider-enforced position, velocity, and
  effort limits.

Raw `VELOCITY` is intentionally unavailable. Enabling attended control obtains
a short root lease. A slider produces commands only while its pointer deadman
is held. Pointer release, pointer cancellation, window focus loss, lease loss,
or an explicit Gravity float request ends manual motion and returns the target
display to measured state. Safe home remains a separate explicit action.

The canonical root resource ID may be returned by the Provider and must be
round-tripped unchanged. Missing `resource_id` and the configured canonical
root ID both select root authority; only a declared child resource selects
actuator-group authority.

## Local collision display

The separate UI process owns a simplified arm-capsule and desktop-plane check.
It evaluates the current measured pose for operator awareness only. It does
not approve a requested range, validate external obstacles, or become an
operational motion planner. Basic independently enforces leases, deadlines,
joint and operational limits, command-mode limits, feedback freshness,
gravity support, and safe-home behavior.

## Calibration configuration

Basic still requires a reviewed, revision-bound arm calibration profile as a
runtime model input. This UI does not create or update that profile. Any future
calibration producer must be a separately designed component with explicit
ownership, validation, candidate activation, rollback, and audit contracts;
it must not be reintroduced as implicit motion inside this development UI.
