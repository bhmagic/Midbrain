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
    & $python -m pip install "motorbridge>=0.4.9"
    if ($LASTEXITCODE -ne 0) { throw "MotorBridge installation failed." }
}
$config = Join-Path $provider "config"
New-Item -ItemType Directory -Force -Path $config | Out-Null
if (-not (Test-Path (Join-Path $config "arm_model.json"))) { Copy-Item (Join-Path $provider "config_templates\arm_model.factory.json") (Join-Path $config "arm_model.json") }
if (-not (Test-Path (Join-Path $config "arm_calibration.json"))) { Copy-Item (Join-Path $provider "config_templates\arm_calibration.initial.json") (Join-Path $config "arm_calibration.json") }
if (-not (Test-Path (Join-Path $config "calibration_collision_model.json"))) { Copy-Item (Join-Path $provider "config_templates\calibration_collision_model.json") (Join-Path $config "calibration_collision_model.json") }
Write-Host "Basic Controller setup complete."
Write-Host "Private Python: $python"
