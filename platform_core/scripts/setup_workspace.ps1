param(
    [string]$PythonLauncher = "py",
    [switch]$SkipCameraBuild,
    [string]$OrbbecIncludeDir = "C:\Program Files\OrbbecSDK 2.8.6\include",
    [string]$OrbbecLibrary = "C:\Program Files\OrbbecSDK 2.8.6\lib\OrbbecSDK.lib",
    [string]$OrbbecBinDir = "C:\Program Files\OrbbecSDK 2.8.6\bin"
)

. (Join-Path $PSScriptRoot "common.ps1")
$workspace = Get-WorkspaceRoot

Write-Host "[1/4] Setting up Manager and Fabric"
& (Join-Path $PSScriptRoot "setup.ps1")

$providerSetup = Join-Path $workspace "providers\orbbec_femto_bolt\scripts\setup.ps1"
if (Test-Path $providerSetup) {
    Write-Host "[2/4] Setting up Orbbec Femto Bolt provider"
    & $providerSetup `
        -PythonLauncher $PythonLauncher `
        -SkipNativeBuild:$SkipCameraBuild `
        -OrbbecIncludeDir $OrbbecIncludeDir `
        -OrbbecLibrary $OrbbecLibrary `
        -OrbbecBinDir $OrbbecBinDir
}
else {
    Write-Host "[2/4] Provider package not present; skipped" -ForegroundColor Yellow
}

$vioSetup = Join-Path $workspace "providers\local_vio\scripts\setup.ps1"
if (Test-Path $vioSetup) {
    Write-Host "[3/4] Setting up Local VIO provider"
    & $vioSetup -PythonLauncher $PythonLauncher
}
else {
    Write-Host "[3/4] Local VIO package not present; skipped" -ForegroundColor Yellow
}

$agentSetup = Join-Path $workspace "test_agent\scripts\setup.ps1"
if (Test-Path $agentSetup) {
    Write-Host "[4/4] Setting up testing agent"
    & $agentSetup -PythonLauncher $PythonLauncher
}
else {
    Write-Host "[4/4] Test-agent package not present; skipped" -ForegroundColor Yellow
}

Write-Host "Workspace setup complete. Existing config files were preserved."
