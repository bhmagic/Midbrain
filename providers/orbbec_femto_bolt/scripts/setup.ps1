param(
    [string]$PythonLauncher = "py",
    [switch]$SkipNativeBuild,
    [string]$OrbbecIncludeDir = "C:\Program Files\OrbbecSDK 2.8.6\include",
    [string]$OrbbecLibrary = "C:\Program Files\OrbbecSDK 2.8.6\lib\OrbbecSDK.lib",
    [string]$OrbbecBinDir = "C:\Program Files\OrbbecSDK 2.8.6\bin"
)

. (Join-Path $PSScriptRoot "common.ps1")
$provider = Get-ProviderRoot
$workspace = Get-WorkspaceRoot
$configDir = Join-Path $workspace "config"
New-Item -ItemType Directory -Force -Path $configDir | Out-Null

$venvPython = Join-Path $workspace ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    if ($PythonLauncher -eq "py") {
        & py -3.11 -m venv (Join-Path $workspace ".venv")
    }
    else {
        & $PythonLauncher -m venv (Join-Path $workspace ".venv")
    }
}
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -e (Join-Path $provider "python")
if ($LASTEXITCODE -ne 0) { throw "Provider Python package installation failed." }

if (-not $SkipNativeBuild) {
    & (Join-Path $PSScriptRoot "build_native.ps1") `
        -OrbbecIncludeDir $OrbbecIncludeDir `
        -OrbbecLibrary $OrbbecLibrary `
        -OrbbecBinDir $OrbbecBinDir
}
else {
    Write-Host "Native CameraHost build skipped."
}

& (Join-Path $PSScriptRoot "diagnose_runtime.ps1") `
    -OrbbecBinDir $OrbbecBinDir

& (Join-Path $PSScriptRoot "register.ps1")
Write-Host "Orbbec Femto Bolt provider setup complete."
Write-Host "Per-frame metadata on Windows may require Orbbec's Administrator registration script."
Write-Host "See providers\orbbec_femto_bolt\docs\WINDOWS_FRAME_METADATA_SETUP.md"

Write-Host "Accelerometer calibration GUI: providers\orbbec_femto_bolt\scripts\run_accelerometer_calibration.ps1"
