# Archived Tutorial: Camera IMU Calibration GUI

This historical tutorial records the pre-portal workflow. Commands and UI
navigation may no longer match the current system. Start from the canonical
[Midbrain Main GUI Portal](../04_MAIN_GUI_PORTAL.md) and enter calibration
through the camera Provider's developer UI instead.

The tutorial uses the standalone calibration GUI as the second example and functional check. It validates the camera Provider's raw IMU access, device identity, calibration storage, quality checks, atomic file replacement, backup behavior, and live reload.

## Safety boundary

The GUI does not immobilize a robot or authorize motion. Secure the camera mechanically. If it is attached to a robot, disable the robot through an independent safe mechanism before calibration.

## Calibration model

For each accelerometer axis:

`corrected = scale × raw + offset`

The workflow captures six strong orientations: `X+`, `X-`, `Y+`, `Y-`, and `Z-`. Each capture averages raw accelerometer samples over the configured interval, normally two seconds.

The solver checks axis dominance, cross-axis magnitude, gravity magnitude, noise, sample count, matrix rank, condition number, residuals, and physical coefficient bounds. A linear solution seeds a damped nonlinear refinement against the gravity-norm equations.

## Prerequisites

1. Complete workspace setup.
2. Start the workspace.
3. Confirm the camera Provider at `http://127.0.0.1:7101` reports `HOT`.
4. Place the camera on a stable surface with enough room to rotate it safely.

## Start the GUI

```powershell
cd C:\Projects\testing_physical_ai
.\providers\orbbec_femto_bolt\scripts\run_accelerometer_calibration.ps1
```

The default GUI is `http://127.0.0.1:8111`.

Optional arguments:

```powershell
.\providers\orbbec_femto_bolt\scripts\run_accelerometer_calibration.ps1 `
  -Port 8111 `
  -CaptureSeconds 2.0 `
  -NoBrowser
```

## Capture procedure

For each requested orientation:

1. Place the camera so the requested axis points strongly upward or downward as shown by the GUI.
2. Keep it motionless.
3. Start capture.
4. Wait for the capture and quality report.
5. Repeat only if the GUI reports weak dominance, excessive noise, or an implausible gravity magnitude.

Complete all six orientations before solving.

## Review and write

Review:

- scale and offset for all three axes
- condition number
- residual error
- quality status
- physical bounds

When accepted, the GUI:

1. identifies the physical device as `orbbec:femto-bolt:<serial>`;
2. backs up the previous calibration with a timestamped filename;
3. writes the new JSON atomically under `config\calibration\devices\orbbec\femto-bolt\<serial>\imu-accelerometer.json`;
4. marks it as a custom calibrated revision;
5. requests live camera Provider reload.

The serial and calibration file are machine-local and must not be committed.

## Functional check after write

- Confirm the Provider reload succeeds.
- Confirm the calibration revision changes in camera status.
- Place the camera still in several orientations and check corrected acceleration magnitude is close to local gravity.
- Restart the workspace and confirm the same serial-bound revision is loaded.
- Confirm VIO runtime bias estimates do not overwrite physical device calibration.

## Automated solver check

```powershell
$env:PYTHONPATH = "$PWD\providers\orbbec_femto_bolt;$PWD\providers\orbbec_femto_bolt\python"
python -m pytest -q .\providers\orbbec_femto_bolt\python\tests
```
