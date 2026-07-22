param([string]$PythonLauncher = "py")

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

. (Join-Path $PSScriptRoot "common.ps1")

$provider = Get-ProviderRoot
$workspace = Get-WorkspaceRoot
$python = Join-Path $provider ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python)) {
    if ($PythonLauncher -eq "py") {
        & py -3.11 -m venv (Join-Path $provider ".venv")
    }
    else {
        & $PythonLauncher -m venv (Join-Path $provider ".venv")
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Creating Provider-local Python environment failed."
    }
}

$cameraSupport = Join-Path $workspace "providers\orbbec_femto_bolt\python"
if (-not (Test-Path (Join-Path $cameraSupport "pyproject.toml"))) {
    throw "Orbbec provider support package is required before FoundationPose setup."
}

& $python -m pip install -e $cameraSupport
if ($LASTEXITCODE -ne 0) {
    throw "Orbbec support package installation failed."
}

& $python -m pip install -e (Join-Path $provider "python")
if ($LASTEXITCODE -ne 0) {
    throw "FoundationPose Provider package installation failed."
}

& (Join-Path $PSScriptRoot "seed_default_models.ps1")
if ($LASTEXITCODE -ne 0) {
    throw "FoundationPose default model seeding failed."
}

& (Join-Path $PSScriptRoot "register.ps1")
if ($LASTEXITCODE -ne 0) {
    throw "FoundationPose Provider registration failed."
}

Write-Host "FoundationPose Provider package setup complete."
Write-Host "Use the clean release installer for the full native NVIDIA runtime installation."
