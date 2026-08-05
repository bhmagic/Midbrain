param(
    [string]$PythonLauncher = "py",
    [switch]$SkipCameraBuild,
    [string]$OrbbecIncludeDir = "C:\Program Files\OrbbecSDK 2.8.6\include",
    [string]$OrbbecLibrary = "C:\Program Files\OrbbecSDK 2.8.6\lib\OrbbecSDK.lib",
    [string]$OrbbecBinDir = "C:\Program Files\OrbbecSDK 2.8.6\bin"
)

. (Join-Path $PSScriptRoot "common.ps1")
$workspace = Get-WorkspaceRoot

function Invoke-ComponentSetup {
    param(
        [int]$Step,
        [string]$Label,
        [string]$RelativePath
    )

    $setupPath = Join-Path $workspace $RelativePath
    if (Test-Path -LiteralPath $setupPath) {
        Write-Host "[$Step/10] $Label"
        & $setupPath -PythonLauncher $PythonLauncher
        if ($LASTEXITCODE -ne 0) {
            throw "$Label failed with exit code $LASTEXITCODE."
        }
    }
    else {
        Write-Host "[$Step/10] $Label package not present; skipped" -ForegroundColor Yellow
    }
}

Write-Host "[1/10] Setting up Manager and Fabric"
& (Join-Path $PSScriptRoot "setup.ps1")

$providerSetup = Join-Path $workspace "providers\orbbec_femto_bolt\scripts\setup.ps1"
if (Test-Path $providerSetup) {
    Write-Host "[2/10] Setting up Orbbec Femto Bolt Provider"
    & $providerSetup `
        -PythonLauncher $PythonLauncher `
        -SkipNativeBuild:$SkipCameraBuild `
        -OrbbecIncludeDir $OrbbecIncludeDir `
        -OrbbecLibrary $OrbbecLibrary `
        -OrbbecBinDir $OrbbecBinDir
}
else {
    Write-Host "[2/10] Orbbec Provider package not present; skipped" -ForegroundColor Yellow
}

Invoke-ComponentSetup 3 "Setting up Local VIO Provider" `
    "providers\local_vio\scripts\setup.ps1"
Invoke-ComponentSetup 4 "Setting up Arm Scene Compiler Provider" `
    "providers\arm_scene_compiler\scripts\setup.ps1"
Invoke-ComponentSetup 5 "Setting up HOT SAM2 Scene Tracker Provider" `
    "providers\sam2_scene_tracker\scripts\setup.ps1"
Invoke-ComponentSetup 6 "Setting up Spatial Registration Skill" `
    "skills\spatial_registration_rgbd\scripts\setup.ps1"
Invoke-ComponentSetup 7 "Setting up Tool Registration Skill" `
    "skills\register_tool_to_control_frame\scripts\setup.ps1"
Invoke-ComponentSetup 8 "Setting up Effector-Front Skill" `
    "skills\locate-effector-front\scripts\setup.ps1"
Invoke-ComponentSetup 9 "Setting up Stationary Alignment Skill" `
    "skills\stationary_world_arm_alignment\scripts\setup.ps1"
Invoke-ComponentSetup 10 "Setting up Test Agent/OpenAI Agents SDK" `
    "test_agent\scripts\setup.ps1"

Write-Host "Workspace setup complete with component-local Python environments. Existing config files were preserved."
