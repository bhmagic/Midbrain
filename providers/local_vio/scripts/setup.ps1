param([string]$PythonLauncher = "py")

. (Join-Path $PSScriptRoot "common.ps1")
$provider = Get-ProviderRoot
$workspace = Get-WorkspaceRoot
$venv = Join-Path $provider ".venv"
$python = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $python)) {
    if ($PythonLauncher -eq "py") { & py -3.11 -m venv $venv }
    else { & $PythonLauncher -m venv $venv }
    if ($LASTEXITCODE -ne 0) { throw "Local VIO virtual environment creation failed." }
}
& $python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "Local VIO pip upgrade failed." }
$cameraSupport = Join-Path $workspace "providers\orbbec_femto_bolt\python"
if (-not (Test-Path (Join-Path $cameraSupport "pyproject.toml"))) {
    throw "Orbbec provider support package is required before Local VIO setup."
}
& $python -m pip install -e $cameraSupport
if ($LASTEXITCODE -ne 0) { throw "Orbbec support package installation failed." }
& $python -m pip install -e (Join-Path $provider "python")
if ($LASTEXITCODE -ne 0) { throw "Local VIO package installation failed." }
& (Join-Path $PSScriptRoot "register.ps1")
Write-Host "Local VIO provider environment ready: $venv"
