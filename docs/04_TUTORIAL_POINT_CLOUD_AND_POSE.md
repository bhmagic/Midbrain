# Tutorial: Mock Agent, Point Cloud, and Pose

This tutorial uses the Test Agent as both an example client and a functional check of the Manager, Fabric, camera Provider, Local VIO Provider, and transform flow.

## What it demonstrates

- A finite initialization Skill requesting persistent Provider capabilities.
- Motion-inhibit coordination through the Manager.
- Fabric consumption of camera bundles, calibration, VIO status, body pose, and transforms.
- World-frame RGB point-cloud accumulation.
- Live camera/body pose visualization.
- Reset/session-epoch handling.
- Independent diagnostics for inertial propagation, visual correction, gravity leveling, and map capture.

## Start the example

From Developer PowerShell:

```powershell
cd C:\Projects\testing_physical_ai
.\platform_core\scripts\run_workspace.ps1
```

Open `http://127.0.0.1:8000` if the browser does not open automatically.

## Functional check

### 1. Provider and service health

Confirm:

- Manager and Fabric health endpoints respond.
- Camera Provider reports `HOT`.
- Local VIO Provider is running or is activated by the initialization Skill.
- RGB, depth, calibration, accelerometer, and gyroscope histories are present.

### 2. Initialization

Keep the camera still. Confirm:

- Initialize Space Cognition reaches `SUCCEEDED`.
- Accelerometer and gyro history counts increase.
- The selected initialization window reaches the required counts.
- The initialization blocker is `none`.
- A session epoch and world frame such as `local_vio/<epoch>` are shown.

### 3. Pose propagation

Rotate the camera moderately. Confirm:

- IMU propagation steps continue increasing between camera frames.
- The camera frustum follows the current pose.
- Visual correction can briefly become stale without freezing inertial pose propagation.
- Gravity changes roll/pitch only when the IMU quiet gate permits it.

### 4. Point cloud

Move through a textured room at moderate speed. Confirm:

- Map Capture reports `CAPTURING`.
- Point count increases.
- RGB points appear in an orthographic isometric world view.
- The orange arrow indicates world down.
- Points fade according to the configured retention period.

Mouse drag orbits the view. The wheel changes scale. The reset-view control returns to the canonical isometric view.

### 5. Reset lifecycle

Place the camera still and select **Force reinitialize origin**. Confirm:

- The session epoch changes.
- Observation sequence remains monotonic.
- Old-epoch points are removed only after the new epoch is accepted.
- Point capture resumes without restarting the workspace.

### 6. Clear-only behavior

Select **Clear point cloud**. Confirm the display clears while the session epoch and VIO state remain unchanged.

## Expected failure classifications

- `WAITING_FOR_INPUTS`: one or more of synchronized RGB-D, calibration, or body pose is missing.
- `PAUSED_UNTIL_VISUAL_TRACKING`: pose is degraded and new map insertion is intentionally paused.
- `BufferRef has expired or slot recycled`: transient dropped frame; the next reference should be acquired.
- Visual correction `STALE`: may be normal while IMU propagation continues; inspect feature support and correction age.

## Automated check

The mock-agent lifecycle tests can be run without the camera:

```powershell
$env:PYTHONPATH = "$PWD\providers\local_vio;$PWD\providers\local_vio\python;$PWD\providers\orbbec_femto_bolt;$PWD\providers\orbbec_femto_bolt\python;$PWD\test_agent\python"
python -m pytest -q .\test_agent\python\tests
```
