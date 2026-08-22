param(
    [string]$PythonLauncher = "py",
    [switch]$WithMotorBridge
)
. (Join-Path $PSScriptRoot "common.ps1")

function Write-JsonUtf8NoBom {
    param(
        [Parameter(Mandatory = $true)][object]$Value,
        [Parameter(Mandatory = $true)][string]$Path
    )
    $payload = ($Value | ConvertTo-Json -Depth 30) + "`n"
    [System.IO.File]::WriteAllText(
        $Path,
        $payload,
        [System.Text.UTF8Encoding]::new($false)
    )
}

function Update-WristEnvelopeInArmModel {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][object]$FactoryModel
    )
    $model = Get-Content -Raw -LiteralPath $Path | ConvertFrom-Json
    foreach ($jointName in @("joint4", "joint5", "joint6")) {
        $source = $FactoryModel.joints | Where-Object { $_.name -eq $jointName } | Select-Object -First 1
        $target = $model.joints | Where-Object { $_.name -eq $jointName } | Select-Object -First 1
        if ($null -eq $source -or $null -eq $target) {
            throw "Cannot migrate $Path because $jointName is unavailable."
        }
        $target.hard_limit_rad = @($source.hard_limit_rad)
        $target.operational_limit_rad = @($source.operational_limit_rad)
        $target.default_calibration_range_rad = @($source.default_calibration_range_rad)
    }
    $sourceGripper = $FactoryModel.joints | Where-Object { $_.name -eq "gripper" } | Select-Object -First 1
    $targetGripper = $model.joints | Where-Object { $_.name -eq "gripper" } | Select-Object -First 1
    if ($null -eq $sourceGripper -or $null -eq $targetGripper) {
        throw "Cannot migrate $Path because the gripper joint is unavailable."
    }
    $targetUpper = [double]$targetGripper.operational_limit_rad[1]
    $sourceUpper = [double]$sourceGripper.operational_limit_rad[1]
    if (
        [math]::Abs($targetUpper - -0.3490658503988659) -ge 0.000000001 -and
        [math]::Abs($targetUpper - -0.17453292519943295) -ge 0.000000001 -and
        [math]::Abs($targetUpper) -ge 0.000000001 -and
        [math]::Abs($targetUpper - $sourceUpper) -ge 0.000000001
    ) {
        throw "Cannot automatically migrate the customized gripper close envelope in $Path."
    }
    $targetHardUpper = [double]$targetGripper.hard_limit_rad[1]
    $sourceHardUpper = [double]$sourceGripper.hard_limit_rad[1]
    if (
        [math]::Abs($targetHardUpper - -0.15707963267948966) -ge 0.000000001 -and
        [math]::Abs($targetHardUpper) -ge 0.000000001 -and
        [math]::Abs($targetHardUpper - $sourceHardUpper) -ge 0.000000001
    ) {
        throw "Cannot automatically migrate the customized gripper hard close boundary in $Path."
    }
    $targetGripper.hard_limit_rad = @($sourceGripper.hard_limit_rad)
    $targetGripper.operational_limit_rad = @($sourceGripper.operational_limit_rad)
    $targetGripper.default_calibration_range_rad = @($sourceGripper.default_calibration_range_rad)
    $targetSpeedCaps = @($model.control.physical_test_pos_vel_cap_rad_s)
    $sourceSpeedCaps = @($FactoryModel.control.physical_test_pos_vel_cap_rad_s)
    if ($targetSpeedCaps.Count -ne 7 -or $sourceSpeedCaps.Count -ne 7) {
        throw "Cannot migrate $Path because the seven-joint POS_VEL cap vector is unavailable."
    }
    $targetGripperSpeedCap = [double]$targetSpeedCaps[6]
    $sourceGripperSpeedCap = [double]$sourceSpeedCaps[6]
    if (
        [math]::Abs($targetGripperSpeedCap - 2.1) -ge 0.000000001 -and
        [math]::Abs($targetGripperSpeedCap - $sourceGripperSpeedCap) -ge 0.000000001
    ) {
        throw "Cannot automatically migrate the customized gripper POS_VEL cap in $Path."
    }
    $targetSpeedCaps[6] = $sourceGripperSpeedCap
    $model.control.physical_test_pos_vel_cap_rad_s = $targetSpeedCaps
    $model.model_revision = $FactoryModel.model_revision
    $model.source_notes = @($FactoryModel.source_notes)
    $appendixKey = "midbrain.skill.locate_arm_base.v1"
    $targetAppendixProperty = $null
    $factoryAppendixProperty = $null
    $modelAppendixProperty = $model.PSObject.Properties["appendix"]
    $factoryModelAppendixProperty = $FactoryModel.PSObject.Properties["appendix"]
    if (
        $null -ne $modelAppendixProperty -and
        $null -ne $factoryModelAppendixProperty -and
        $null -ne $modelAppendixProperty.Value -and
        $null -ne $factoryModelAppendixProperty.Value
    ) {
        $targetAppendixProperty = $modelAppendixProperty.Value.PSObject.Properties[$appendixKey]
        $factoryAppendixProperty = $factoryModelAppendixProperty.Value.PSObject.Properties[$appendixKey]
    }
    if ($null -ne $targetAppendixProperty -and $null -ne $factoryAppendixProperty) {
        $targetAppendix = $targetAppendixProperty.Value
        $factoryAppendix = $factoryAppendixProperty.Value
        $meshRelativePath = [string]$targetAppendix.mesh.path
        if (-not $meshRelativePath -or [System.IO.Path]::IsPathRooted($meshRelativePath)) {
            throw "Cannot migrate the localization mesh digest in $Path because its path is invalid."
        }
        $workspaceRoot = (Resolve-Path -LiteralPath (Get-WorkspaceRoot)).Path
        $meshPath = [System.IO.Path]::GetFullPath((Join-Path $workspaceRoot $meshRelativePath))
        $workspacePrefix = $workspaceRoot.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
        if (-not $meshPath.StartsWith($workspacePrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Cannot migrate the localization mesh digest outside the workspace: $meshPath"
        }
        if (-not (Test-Path -LiteralPath $meshPath -PathType Leaf)) {
            throw "Cannot migrate the localization mesh digest because the asset is missing: $meshPath"
        }
        if ($meshRelativePath -eq [string]$factoryAppendix.mesh.path) {
            $meshText = [System.IO.File]::ReadAllText(
                $meshPath,
                [System.Text.UTF8Encoding]::new($false)
            )
            $normalizedMeshText = $meshText.Replace("`r`n", "`n")
            if ($normalizedMeshText -cne $meshText) {
                [System.IO.File]::WriteAllText(
                    $meshPath,
                    $normalizedMeshText,
                    [System.Text.UTF8Encoding]::new($false)
                )
            }
        }
        $meshDigest = (Get-FileHash -LiteralPath $meshPath -Algorithm SHA256).Hash.ToLowerInvariant()
        $targetAppendix.mesh.sha256 = $meshDigest
        if ($null -ne $targetAppendix.mesh.preview) {
            $targetAppendix.mesh.preview.mesh_sha256 = $meshDigest
        }
    }
    Write-JsonUtf8NoBom -Value $model -Path $Path
}

function Update-WristEnvelopeInCalibration {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][object]$InitialCalibration
    )
    $calibration = Get-Content -Raw -LiteralPath $Path | ConvertFrom-Json
    $changed = $false
    foreach ($jointName in @("joint4", "joint5", "joint6")) {
        $source = $InitialCalibration.joints | Where-Object { $_.name -eq $jointName } | Select-Object -First 1
        $target = $calibration.joints | Where-Object { $_.name -eq $jointName } | Select-Object -First 1
        if ($null -eq $source -or $null -eq $target) {
            throw "Cannot migrate $Path because $jointName is unavailable."
        }
        $before = (@($target.operational_limit_rad) -join ",")
        $after = (@($source.operational_limit_rad) -join ",")
        if ($before -ne $after) {
            $target.operational_limit_rad = @($source.operational_limit_rad)
            $changed = $true
        }
    }
    $sourceGripper = $InitialCalibration.joints | Where-Object { $_.name -eq "gripper" } | Select-Object -First 1
    $targetGripper = $calibration.joints | Where-Object { $_.name -eq "gripper" } | Select-Object -First 1
    if ($null -eq $sourceGripper -or $null -eq $targetGripper) {
        throw "Cannot migrate $Path because the gripper joint is unavailable."
    }
    $targetUpper = [double]$targetGripper.operational_limit_rad[1]
    $sourceUpper = [double]$sourceGripper.operational_limit_rad[1]
    if (
        [math]::Abs($targetUpper - -0.3490658503988659) -ge 0.000000001 -and
        [math]::Abs($targetUpper - -0.17453292519943295) -ge 0.000000001 -and
        [math]::Abs($targetUpper) -ge 0.000000001 -and
        [math]::Abs($targetUpper - $sourceUpper) -ge 0.000000001
    ) {
        throw "Cannot automatically migrate the customized gripper calibration envelope in $Path."
    }
    if ([math]::Abs($targetUpper - $sourceUpper) -ge 0.000000001) {
        $targetGripper.operational_limit_rad = @($sourceGripper.operational_limit_rad)
        $changed = $true
    }
    $targetGripperSpeedCap = [double]$targetGripper.provider_velocity_cap_rad_s
    $sourceGripperSpeedCap = [double]$sourceGripper.provider_velocity_cap_rad_s
    if (
        [math]::Abs($targetGripperSpeedCap - 2.1) -ge 0.000000001 -and
        [math]::Abs($targetGripperSpeedCap - $sourceGripperSpeedCap) -ge 0.000000001
    ) {
        throw "Cannot automatically migrate the customized gripper velocity cap in $Path."
    }
    $speedChanged = (
        [math]::Abs($targetGripperSpeedCap - $sourceGripperSpeedCap) -ge 0.000000001
    )
    if ($speedChanged) {
        $targetGripper.provider_velocity_cap_rad_s = $sourceGripperSpeedCap
        $changed = $true
    }
    if ($changed -and -not ([string]$calibration.calibration_revision).EndsWith("-gripper-close-plus12deg-20260820")) {
        $calibration.calibration_revision = "$( $calibration.calibration_revision )-gripper-close-plus12deg-20260820"
    }
    if ($speedChanged -and -not ([string]$calibration.calibration_revision).EndsWith("-gripper-speed-4rads-20260820")) {
        $calibration.calibration_revision = "$( $calibration.calibration_revision )-gripper-speed-4rads-20260820"
    }
    Write-JsonUtf8NoBom -Value $calibration -Path $Path
}

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
$factoryModelPath = Join-Path $provider "config_templates\arm_model.factory.json"
$initialCalibrationPath = Join-Path $provider "config_templates\arm_calibration.initial.json"
$factoryModel = Get-Content -Raw -LiteralPath $factoryModelPath | ConvertFrom-Json
$initialCalibration = Get-Content -Raw -LiteralPath $initialCalibrationPath | ConvertFrom-Json
$legacyArmModel = Join-Path $config "arm_model.json"
if (-not (Test-Path $legacyArmModel)) { Copy-Item $factoryModelPath $legacyArmModel }
Update-WristEnvelopeInArmModel -Path $legacyArmModel -FactoryModel $factoryModel
$armProfiles = Join-Path $config "arm_profiles"
New-Item -ItemType Directory -Force -Path $armProfiles | Out-Null
$defaultArmProfile = Join-Path $armProfiles "rebot_arm_b601_dm.v1.json"
if (-not (Test-Path -LiteralPath $defaultArmProfile)) {
    Copy-Item -LiteralPath $legacyArmModel -Destination $defaultArmProfile
}
Update-WristEnvelopeInArmModel -Path $defaultArmProfile -FactoryModel $factoryModel
$activeCalibration = Join-Path $config "arm_calibration.json"
if (-not (Test-Path $activeCalibration)) { Copy-Item $initialCalibrationPath $activeCalibration }
Update-WristEnvelopeInCalibration -Path $activeCalibration -InitialCalibration $initialCalibration
if (-not (Test-Path (Join-Path $config "calibration_collision_model.json"))) { Copy-Item (Join-Path $provider "config_templates\calibration_collision_model.json") (Join-Path $config "calibration_collision_model.json") }
$workspace = Get-WorkspaceRoot
$assemblyTemplate = Join-Path $workspace "config\robot_assemblies\primary_manipulator.example.json"
if (Test-Path -LiteralPath $assemblyTemplate) {
    $assemblyDirectory = Join-Path $workspace "config\robot_assemblies"
    $assemblyConfig = Join-Path $assemblyDirectory "primary_manipulator.json"
    New-Item -ItemType Directory -Force -Path $assemblyDirectory | Out-Null
    if (-not (Test-Path -LiteralPath $assemblyConfig)) {
        $selection = Get-Content -Raw -LiteralPath $assemblyTemplate | ConvertFrom-Json
        $calibration = Get-Content -Raw -LiteralPath $activeCalibration | ConvertFrom-Json
        $selection.profiles.calibration.expected_revision = $calibration.calibration_revision
        Write-JsonUtf8NoBom -Value $selection -Path $assemblyConfig
        Write-Host "Created active robot assembly selection: $assemblyConfig"
    }
    else {
        $selection = Get-Content -Raw -LiteralPath $assemblyConfig | ConvertFrom-Json
        if ($selection.profiles.arm_model.relative_path -eq "config/arm_model.json") {
            $selection.profiles.arm_model.relative_path = "config/arm_profiles/rebot_arm_b601_dm.v1.json"
            Write-Host "Migrated the active arm-model selection to the arm-profile registry: $assemblyConfig"
        }
        $calibration = Get-Content -Raw -LiteralPath $activeCalibration | ConvertFrom-Json
        $selection.profiles.arm_model.expected_revision = $factoryModel.model_revision
        $selection.profiles.calibration.expected_revision = $calibration.calibration_revision
        $selection.profiles.collision_geometry.expected_revision = "rebot-b601-dm-arm-capsules-v3"
        $mountedEffectorPath = [string]$selection.profiles.mounted_effector.relative_path
        if ($mountedEffectorPath -eq "profiles/effectors/rebot_b601_dm_bare_gripper.v2.json") {
            $selection.profiles.mounted_effector.expected_revision = "rebot-b601-dm-bare-gripper-v5"
            $selection.assembly_revision = ([string]$selection.assembly_revision) -replace "rebot-b601-dm-bare-gripper-v[3-4]", "rebot-b601-dm-bare-gripper-v5"
        }
        elseif ($mountedEffectorPath -eq "profiles/effectors/rebot_b601_dm_5_inch_blade.v1.json") {
            $selection.profiles.mounted_effector.expected_revision = "rebot-b601-dm-5-inch-blade-v5"
            $selection.assembly_revision = ([string]$selection.assembly_revision) -replace "rebot-b601-dm-5-inch-blade-v[3-4]", "rebot-b601-dm-5-inch-blade-v5"
        }
        if ([string]$selection.assembly_revision -match "-grip-control-v[1-5]$") {
            $selection.assembly_revision = ([string]$selection.assembly_revision) -replace "-grip-control-v[1-5]$", "-grip-control-v6"
        }
        elseif ([string]$selection.assembly_revision -match "^rebot-b601-dm-5-inch-blade-development-v[3-7]$") {
            $selection.assembly_revision = "rebot-b601-dm-5-inch-blade-development-v8"
        }
        Write-JsonUtf8NoBom -Value $selection -Path $assemblyConfig
    }
}
Write-Host "Basic Controller setup complete."
Write-Host "Private Python: $python"
