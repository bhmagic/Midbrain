param(
    [string]$PythonLauncher = "py",
    [switch]$WithMotorBridge
)
. (Join-Path $PSScriptRoot "common.ps1")
$provider = Get-ProviderRoot
$venv = Join-Path $provider ".venv"
$python = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $python)) {
    if ($PythonLauncher -eq "py") { & py -3.11 -m venv $venv } else { & $PythonLauncher -m venv $venv }
    if ($LASTEXITCODE -ne 0) { throw "Could not create the Basic Controller Python environment. Python 3.11 is required." }
}
& $python -m pip install --upgrade pip setuptools wheel
if ($LASTEXITCODE -ne 0) { throw "Could not update packaging tools." }
& $python -m pip install -e (Join-Path $provider "python")
if ($LASTEXITCODE -ne 0) { throw "Basic Controller package installation failed." }
if ($WithMotorBridge) {
    $workspace = Get-WorkspaceRoot
    $motorBridgeCommit = "e9ec70e455f5d37dd7170ad13532daf288759152"
    $motorBridgeSource = Join-Path $workspace ".artifact_work\motorbridge"
    $motorBridgePatch = Join-Path $provider "third_party\motorbridge-state-sample.patch"
    if (-not (Test-Path (Join-Path $motorBridgeSource ".git"))) {
        New-Item -ItemType Directory -Force -Path (Split-Path $motorBridgeSource) | Out-Null
        & git clone --filter=blob:none --no-checkout https://github.com/motorbridge/motorbridge.git $motorBridgeSource
        if ($LASTEXITCODE -ne 0) { throw "Could not clone the reviewed MotorBridge source." }
        & git -C $motorBridgeSource checkout $motorBridgeCommit
        if ($LASTEXITCODE -ne 0) { throw "Could not check out reviewed MotorBridge commit $motorBridgeCommit." }
    }
    $actualCommit = (& git -C $motorBridgeSource rev-parse HEAD).Trim()
    if ($actualCommit -ne $motorBridgeCommit) {
        throw "MotorBridge source is at $actualCommit; expected reviewed commit $motorBridgeCommit."
    }
    # Windows line-ending behavior can make reverse-apply probing reject an
    # already-applied patch. Accept only an exact normalized tracked diff.
    $expectedPatch = ((Get-Content -Raw -LiteralPath $motorBridgePatch) -replace "`r`n", "`n").TrimEnd()
    $actualPatch = ((& git -C $motorBridgeSource diff --binary) -join "`n").TrimEnd()
    $patchAlreadyApplied = $actualPatch -ceq $expectedPatch
    if (-not $patchAlreadyApplied) {
        if ($actualPatch) {
            throw "MotorBridge source contains unrelated changes; preserve or remove them before setup."
        }
        & git -C $motorBridgeSource apply --check $motorBridgePatch
        if ($LASTEXITCODE -ne 0) { throw "The reviewed MotorBridge freshness patch does not apply cleanly." }
        & git -C $motorBridgeSource apply $motorBridgePatch
        if ($LASTEXITCODE -ne 0) { throw "Could not apply the reviewed MotorBridge freshness patch." }
        $actualPatch = ((& git -C $motorBridgeSource diff --binary) -join "`n").TrimEnd()
    }
    if ($actualPatch -cne $expectedPatch) {
        throw "MotorBridge source differs from the reviewed tracked patch."
    }
    & cargo build --manifest-path (Join-Path $motorBridgeSource "Cargo.toml") -p motor_abi -p ws_gateway --release
    if ($LASTEXITCODE -ne 0) { throw "Patched MotorBridge native build failed." }
    $env:MOTORBRIDGE_LIB = Join-Path $motorBridgeSource "target\release\motor_abi.dll"
    $env:MOTORBRIDGE_WS_GATEWAY_BIN = Join-Path $motorBridgeSource "target\release\ws_gateway.exe"
    & $python -m pip install --force-reinstall (Join-Path $motorBridgeSource "bindings\python")
    if ($LASTEXITCODE -ne 0) { throw "Patched MotorBridge Python installation failed." }
    & $python -c "import motorbridge; assert hasattr(motorbridge.Motor, 'get_state_sample'); print('MotorBridge freshness API:', motorbridge.__version__)"
    if ($LASTEXITCODE -ne 0) { throw "Installed MotorBridge is missing the reviewed freshness API." }
}
$config = Join-Path $provider "config"
New-Item -ItemType Directory -Force -Path $config | Out-Null
if (-not (Test-Path (Join-Path $config "arm_model.json"))) { Copy-Item (Join-Path $provider "config_templates\arm_model.factory.json") (Join-Path $config "arm_model.json") }
if (-not (Test-Path (Join-Path $config "arm_calibration.json"))) { Copy-Item (Join-Path $provider "config_templates\arm_calibration.initial.json") (Join-Path $config "arm_calibration.json") }
if (-not (Test-Path (Join-Path $config "calibration_collision_model.json"))) { Copy-Item (Join-Path $provider "config_templates\calibration_collision_model.json") (Join-Path $config "calibration_collision_model.json") }
$workspace = Get-WorkspaceRoot
$assemblyTemplate = Join-Path $workspace "config\robot_assemblies\primary_manipulator.example.json"
if (Test-Path -LiteralPath $assemblyTemplate) {
    $assemblyDirectory = Join-Path $workspace "config\robot_assemblies"
    $assemblyConfig = Join-Path $assemblyDirectory "primary_manipulator.json"
    New-Item -ItemType Directory -Force -Path $assemblyDirectory | Out-Null
    if (-not (Test-Path -LiteralPath $assemblyConfig)) {
        $selection = Get-Content -Raw -LiteralPath $assemblyTemplate | ConvertFrom-Json
        $calibration = Get-Content -Raw -LiteralPath (Join-Path $config "arm_calibration.json") | ConvertFrom-Json
        $selection.profiles.calibration.expected_revision = $calibration.calibration_revision
        $selection | ConvertTo-Json -Depth 30 | Set-Content -LiteralPath $assemblyConfig -Encoding utf8
        Write-Host "Created active robot assembly selection: $assemblyConfig"
    }
}
Write-Host "Basic Controller setup complete."
Write-Host "Private Python: $python"
