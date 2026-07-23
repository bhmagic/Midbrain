# reBot Arm DM Basic Controller: Windows install, compile, and run

This procedure assumes the Physical AI workspace is already located at:

`C:\Projects\testing_physical_ai`

The Basic Controller is installed at:

`C:\Projects\testing_physical_ai\providers\rebot_arm_dm`

It owns this private Python environment:

`C:\Projects\testing_physical_ai\providers\rebot_arm_dm\.venv`

The workspace environment at `C:\Projects\testing_physical_ai\.venv` is not used by the Basic Controller.

## 1. Open the workspace

Open a new PowerShell window. A Conda `(base)` prompt is acceptable because every controller script calls the private virtual environment directly.

```powershell
cd C:\Projects\testing_physical_ai
Get-ChildItem
```

Expected existing folders include `config`, `contracts`, `platform_core`, `providers`, and `test_agent`.

## 2. Install the controller files

Extract the workspace overlay ZIP into the workspace root. The ZIP already contains the `providers\rebot_arm_dm` prefix.

```powershell
cd C:\Projects\testing_physical_ai
Expand-Archive -Path C:\Path\To\physical_ai_rebot_arm_dm_basic_controller_v0_1_2_workspace_overlay.zip -DestinationPath . -Force
cd .\providers\rebot_arm_dm
```

Confirm the entry point and scripts exist:

```powershell
Get-ChildItem
Get-ChildItem .\scripts
```

## 3. Check Python 3.11

```powershell
py -0p
py -3.11 --version
```

If `py -3.11` is unavailable, install 64-bit Python 3.11 and enable the Python launcher. Do not create the environment with Python 3.12 or a workspace-shared interpreter.

## 4. Create the private environment and install dependencies

For simulation only:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
```

For real hardware, install MotorBridge at the same time:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1 -WithMotorBridge
```

Confirm the controller uses its own interpreter:

```powershell
.\.venv\Scripts\python.exe -c "import sys; print(sys.executable)"
.\.venv\Scripts\python.exe -c "import numpy; import rebot_arm_dm_provider; print('Basic Controller imports OK')"
```

The printed path must end with:

`testing_physical_ai\providers\rebot_arm_dm\.venv\Scripts\python.exe`

## 5. Compile and test

The provider is Python, so there is no native C++ compilation step. `verify.ps1` byte-compiles the Python sources and runs the unit tests.

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\verify.ps1
```

Expected final line:

`Basic Controller verification passed.`

To build a distributable wheel:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build_wheel.ps1
Get-ChildItem .\dist
```

## 6. Run the complete simulation first

The simplest simulation command starts the provider and calibration GUI together:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_simulation_demo.ps1
```

The browser GUI normally opens at:

`http://127.0.0.1:8792`

The simulated Basic Controller listens at:

`http://127.0.0.1:8791`

For two separate terminals, use:

Terminal 1:

```powershell
cd C:\Projects\testing_physical_ai\providers\rebot_arm_dm
powershell -ExecutionPolicy Bypass -File .\scripts\run_provider.ps1 -Simulate
```

Terminal 2:

```powershell
cd C:\Projects\testing_physical_ai\providers\rebot_arm_dm
powershell -ExecutionPolicy Bypass -File .\scripts\run_calibration.ps1
```

Stop the GUI with `Ctrl+C`, then stop the provider with one `Ctrl+C`. In simulation, the first stop request follows the graceful safe-home path.

## 7. Find and verify the Damiao COM port

The supplied Unity bridge used `COM3` at `921600` baud. Treat `COM3` as the default, but verify it on the current computer.

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\show_com_ports.ps1
```

For more Windows device information:

```powershell
Get-CimInstance Win32_SerialPort | Select-Object DeviceID, Name, Description
```

Disconnecting and reconnecting the Damiao USB adapter can help identify which COM entry belongs to the arm.

## 8. Scan the seven motors without moving the arm

Make sure the arm is powered, the USB adapter is connected, and no other program—including the Unity bridge—is using the COM port.

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\scan_motors.ps1 -Port COM3
```

Expected IDs are motors `1` through `7`, with feedback IDs `17` through `23`. A known-good scan from the Unity setup found seven motors.

If the adapter is not `COM3`, substitute the discovered port in every following command.

## 9. Start real hardware in read-only mode

Mechanically support the arm before the first connection. Keep the emergency stop accessible. Do not open the calibration GUI yet.

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_provider.ps1 -Port COM3
```

This connects the device but does not grant the calibration GUI hardware-motion permission.

In another terminal, verify health and measured state:

```powershell
Invoke-RestMethod http://127.0.0.1:8791/health | ConvertTo-Json -Depth 10
Invoke-RestMethod http://127.0.0.1:8791/v1/arm/state | ConvertTo-Json -Depth 10
```

Check that all seven joint positions update and that signs and values match the physical arm.

Stop with one `Ctrl+C`. The provider attempts safe-home, powered settling, motor disable, and exit. If safe-home cannot be confirmed, it retains powered gravity-float and remains alive when possible; use an external emergency stop only when continued powered operation is unsafe.

## 10. Enable the calibration GUI for hardware

Only after motor IDs, joint signs, zeros, limits, gravity behavior, and safe-home clearance have been reviewed:

Terminal 1:

```powershell
cd C:\Projects\testing_physical_ai\providers\rebot_arm_dm
powershell -ExecutionPolicy Bypass -File .\scripts\run_provider.ps1 -Port COM3 -AllowHardwareCalibration
```

Terminal 2:

```powershell
cd C:\Projects\testing_physical_ai\providers\rebot_arm_dm
powershell -ExecutionPolicy Bypass -File .\scripts\run_calibration.ps1
```

Open `http://127.0.0.1:8792` if the browser does not open automatically.

Before the first command:

- Confirm the rendered pose matches the physical pose.
- Reduce each temporary calibration angle range.
- Use low velocity and torque limits.
- Confirm the desktop plane and the no-other-obstacles warning.
- Test one joint at a time.
- Confirm deadman release enters gravity-float.
- Confirm graceful termination returns to safe-home.

## 11. Register with Manager and Fabric

Registration writes or updates `C:\Projects\testing_physical_ai\config\providers.json`.

```powershell
cd C:\Projects\testing_physical_ai\providers\rebot_arm_dm
powershell -ExecutionPolicy Bypass -File .\scripts\register.ps1
```

The generated Manager entry launches:

`C:\Projects\testing_physical_ai\providers\rebot_arm_dm\.venv\Scripts\python.exe`

It does not use `${PHYSICAL_AGENT_PYTHON}` or the workspace `.venv`.

Review the registration:

```powershell
Get-Content C:\Projects\testing_physical_ai\config\providers.json -Raw
```

Keep `auto_start` set to `false` until hardware validation is complete.

Start the workspace normally:

```powershell
cd C:\Projects\testing_physical_ai
powershell -ExecutionPolicy Bypass -File .\platform_core\scripts\run_workspace.ps1
```

The Manager is at `http://127.0.0.1:7001`, Fabric at `http://127.0.0.1:7002`, and the Basic Controller at `http://127.0.0.1:8791` after Manager starts it.

## 12. Normal shutdown

For a directly launched provider, press `Ctrl+C` once. The Basic Controller should:

1. Stop accepting new commands.
2. Enter gravity-float as needed.
3. Run safe-home.
4. Disable the motors after home is confirmed.
5. Release `COM3`.

For a Manager-controlled provider, request `WARM` or normal stop through the Manager so the same graceful sequence can run. Do not close the PowerShell window or cut arm power as the normal shutdown method.

A process kill, USB loss, or host power loss can prevent safe-home. Reserve those actions for an unsafe or failed graceful shutdown.

## 13. Common problems

### COM port is busy

Close Unity, old Python tests, serial monitors, and any previous provider process. Only the Basic Controller may own the Damiao port.

### `motorbridge-cli.exe` is missing

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1 -WithMotorBridge
```

### Wrong Python environment

```powershell
.\.venv\Scripts\python.exe -c "import sys; print(sys.executable)"
```

The path must be inside `providers\rebot_arm_dm\.venv`.

### Provider health URL does not respond

Check whether port `8791` is already used:

```powershell
Get-NetTCPConnection -LocalPort 8791 -ErrorAction SilentlyContinue
```

Use another provider port when necessary:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_provider.ps1 -Simulate -ListenPort 8891
powershell -ExecutionPolicy Bypass -File .\scripts\run_calibration.ps1 -ProviderUrl http://127.0.0.1:8891 -Port 8892
```

### PowerShell blocks script execution

Every example uses `-ExecutionPolicy Bypass` for that process only. No system-wide execution-policy change is required.
