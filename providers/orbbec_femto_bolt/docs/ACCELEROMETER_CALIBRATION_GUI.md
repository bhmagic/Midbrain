# Femto Bolt Six-Position Accelerometer Calibration GUI

This utility is a standalone local GUI. It is not an agent Skill and does not require an AI model. It reads raw accelerometer samples directly from the Femto Bolt CameraHost shared-memory ring, averages each pose for two seconds, solves a device-specific diagonal scale and offset model, and writes the persistent calibration JSON.

## Safety and motion

The calibration assumes the camera is completely stationary during every capture. The utility does not command robot motion or acquire a motion-inhibit lease. On a future assembled robot, disable or mechanically secure every actuator before starting this procedure.

## Prerequisites

1. Build and install the Femto Bolt Provider.
2. Connect the target Femto Bolt.
3. Start the workspace so the camera Provider is `HOT`.
4. Confirm the camera serial number is visible.

From the workspace root:

```powershell
.\platform_core\scripts\run_workspace.ps1
.\providers\orbbec_femto_bolt\scripts\run_accelerometer_calibration.ps1
```

The utility opens `http://127.0.0.1:8111`. Keep the PowerShell window open while using the GUI.

## Capture procedure

Capture all six requested states:

1. `+X`: raw X is strongly positive and Y/Z are small.
2. `-X`: raw X is strongly negative and Y/Z are small.
3. `+Y`: raw Y is strongly positive and X/Z are small.
4. `-Y`: raw Y is strongly negative and X/Z are small.
5. `+Z`: raw Z is strongly positive and X/Y are small.
6. `-Z`: raw Z is strongly negative and X/Y are small.

The accelerometer reports specific force. Depending on the physical marking and mount, the surface that must face upward may not be obvious. Use the live raw values in the GUI: the requested axis should approach the requested sign with magnitude near `9.80665 m/s²`.

For each state:

1. Place the camera securely.
2. Wait for visible vibration to stop.
3. Click **Capture 2 seconds**.
4. Do not touch the camera until the capture finishes.
5. Retake the state if the GUI rejects motion, weak alignment, cross-axis acceleration, or too few samples.

After all six states are accepted, click **Solve and write calibration**.

## Mathematical model

The persisted correction is diagonal affine calibration:

`corrected_i = scale_i * raw_i + offset_i`

For each static pose `k`, the corrected acceleration magnitude should equal standard gravity:

`sum_i (scale_i * x_ki + offset_i)^2 = g^2`

Write `scale_i = 1 + delta_i` and discard second-order terms in `delta_i` and `offset_i`. The first-order equation is:

`sum_i (x_ki^2 + 2*delta_i*x_ki^2 + 2*offset_i*x_ki) = g^2`

Six poses therefore provide a linear system for:

`[delta_x, delta_y, delta_z, offset_x, offset_y, offset_z]`

The utility solves that linear system first. It then uses the result as the starting point for damped Gauss-Newton refinement against the original nonlinear norm equations. Strong positive and negative alignment for each axis keeps the six-equation design matrix well-conditioned.

This model corrects independent per-axis scale and offset. It does not estimate cross-axis coupling, non-orthogonality, temperature coefficients, vibration response, or a full 3×3 calibration matrix.

## Output

The file is written to:

`config\calibration\devices\orbbec\femto-bolt\<serial>\imu-accelerometer.json`

The previous file is backed up in the same directory before replacement. The new document is marked `CUSTOM_CALIBRATED` and records:

- Device identity and serial number.
- Scale and offset vectors.
- Six averaged capture states and sample statistics.
- Standard gravity used by the fit.
- Linearized seed and nonlinear refined solution.
- Residuals and design-matrix condition number.
- Capture temperature.
- Creation time and method.

After writing, the utility requests `POST /v1/control/reload-calibration` from the running camera Provider. If reload is unavailable, the file is still saved; restart the camera Provider before relying on the new values.

## Reverting

Stop the workspace, replace `imu-accelerometer.json` with the desired `imu-accelerometer.before-<timestamp>.json` backup, then restart the workspace. Do not copy a calibration file between different camera serial numbers.
