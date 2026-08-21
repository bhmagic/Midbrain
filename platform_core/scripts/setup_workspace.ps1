param(
    [string]$PythonLauncher = "",
    [switch]$SkipCameraBuild,
    [string]$OrbbecIncludeDir = "C:\Program Files\OrbbecSDK 2.8.6\include",
    [string]$OrbbecLibrary = "C:\Program Files\OrbbecSDK 2.8.6\lib\OrbbecSDK.lib",
    [string]$OrbbecBinDir = "C:\Program Files\OrbbecSDK 2.8.6\bin"
)

. (Join-Path $PSScriptRoot "common.ps1")
$workspace = Get-WorkspaceRoot
$setupStepCount = 19

function Resolve-Python311Launcher {
    param([string]$RequestedLauncher)

    if (
        -not [string]::IsNullOrWhiteSpace($RequestedLauncher) -and
        [System.IO.Path]::GetFileNameWithoutExtension($RequestedLauncher) -ne "py"
    ) {
        $requestedCommand = Get-Command `
            $RequestedLauncher `
            -CommandType Application `
            -ErrorAction SilentlyContinue
        if ($null -eq $requestedCommand) {
            throw "Python launcher was not found: $RequestedLauncher"
        }
        & $requestedCommand.Source -c (
            "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)"
        )
        if ($LASTEXITCODE -ne 0) {
            throw "Workspace component environments require Python 3.11."
        }
        return $requestedCommand.Source
    }

    $pyCommands = @(
        Get-Command py -CommandType Application -All -ErrorAction SilentlyContinue
    )
    foreach ($pyCommand in $pyCommands) {
        $previousPreference = $ErrorActionPreference
        $ErrorActionPreference = "SilentlyContinue"
        $resolvedPaths = @(
            & $pyCommand.Source -3.11 -c "import sys; print(sys.executable)" 2>$null
        )
        $launcherExitCode = $LASTEXITCODE
        $ErrorActionPreference = $previousPreference
        if ($launcherExitCode -eq 0 -and $resolvedPaths.Count -gt 0) {
            $resolvedPath = [string]$resolvedPaths[-1]
            if (Test-Path -LiteralPath $resolvedPath -PathType Leaf) {
                return $resolvedPath
            }
        }
    }

    $pythonCommands = @(
        Get-Command python -CommandType Application -All -ErrorAction SilentlyContinue
    )
    foreach ($pythonCommand in $pythonCommands) {
        & $pythonCommand.Source -c (
            "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)"
        )
        if ($LASTEXITCODE -eq 0) {
            return $pythonCommand.Source
        }
    }

    throw "Python 3.11 is required. Pass -PythonLauncher with a working executable."
}

$PythonLauncher = Resolve-Python311Launcher $PythonLauncher

function Invoke-ComponentSetup {
    param(
        [int]$Step,
        [string]$Label,
        [string]$RelativePath
    )

    $setupPath = Join-Path $workspace $RelativePath
    if (Test-Path -LiteralPath $setupPath) {
        Write-Host "[$Step/$setupStepCount] $Label"
        & $setupPath -PythonLauncher $PythonLauncher
        if ($LASTEXITCODE -ne 0) {
            throw "$Label failed with exit code $LASTEXITCODE."
        }
    }
    else {
        Write-Host "[$Step/$setupStepCount] $Label package not present; skipped" -ForegroundColor Yellow
    }
}

Write-Host "[1/$setupStepCount] Setting up Manager and Fabric"
& (Join-Path $PSScriptRoot "setup.ps1")

$providerSetup = Join-Path $workspace "providers\orbbec_femto_bolt\scripts\setup.ps1"
if (Test-Path $providerSetup) {
    Write-Host "[2/$setupStepCount] Setting up Orbbec Femto Bolt Provider"
    & $providerSetup `
        -PythonLauncher $PythonLauncher `
        -SkipNativeBuild:$SkipCameraBuild `
        -OrbbecIncludeDir $OrbbecIncludeDir `
        -OrbbecLibrary $OrbbecLibrary `
        -OrbbecBinDir $OrbbecBinDir
}
else {
    Write-Host "[2/$setupStepCount] Orbbec Provider package not present; skipped" -ForegroundColor Yellow
}

Invoke-ComponentSetup 3 "Setting up Local VIO Provider" `
    "providers\local_vio\scripts\setup.ps1"
Invoke-ComponentSetup 4 "Setting up Arm Scene Compiler Provider" `
    "providers\arm_scene_compiler\scripts\setup.ps1"
Invoke-ComponentSetup 5 "Setting up HOT SAM2 Scene Tracker Provider" `
    "providers\sam2_scene_tracker\scripts\setup.ps1"
Invoke-ComponentSetup 6 "Setting up FoundationPose Known-Object Pose Provider" `
    "providers\foundation_pose\scripts\setup.ps1"
Invoke-ComponentSetup 7 "Setting up Contact Provider" `
    "providers\rebot_arm_contact\scripts\setup.ps1"
Invoke-ComponentSetup 8 "Setting up Grip Provider" `
    "providers\rebot_arm_grip\scripts\setup.ps1"
Invoke-ComponentSetup 9 "Setting up Grip Work Runtime" `
    "skills\grip_work_runtime\scripts\setup.ps1"
Invoke-ComponentSetup 10 "Setting up Generic Grip Skill" `
    "skills\grip\scripts\setup.ps1"
Invoke-ComponentSetup 11 "Setting up Scrap Grip Skill" `
    "skills\grip-object\scripts\setup.ps1"
Invoke-ComponentSetup 12 "Setting up Move Carried Object Skill" `
    "skills\move-carried-object\scripts\setup.ps1"
Invoke-ComponentSetup 13 "Setting up Let Go Skill" `
    "skills\let-go\scripts\setup.ps1"
Invoke-ComponentSetup 14 "Setting up Lay Flat Skill" `
    "skills\lay-flat\scripts\setup.ps1"
Invoke-ComponentSetup 15 "Setting up Spatial Registration Skill" `
    "skills\spatial_registration_rgbd\scripts\setup.ps1"
Invoke-ComponentSetup 16 "Setting up Tool Registration Skill" `
    "skills\register_tool_to_control_frame\scripts\setup.ps1"
Invoke-ComponentSetup 17 "Setting up Effector-Front Skill" `
    "skills\locate-effector-front\scripts\setup.ps1"
Invoke-ComponentSetup 18 "Setting up Locate Arm Base Skill" `
    "skills\locate_arm_base\scripts\setup.ps1"
Invoke-ComponentSetup 19 "Setting up Test Agent/OpenAI Agents SDK" `
    "test_agent\scripts\setup.ps1"

Write-Host "Workspace setup complete with component-local Python environments. Existing config files were preserved."
