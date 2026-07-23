param([string]$PythonLauncher = "py")
. (Join-Path $PSScriptRoot "common.ps1")
$root = Get-ProviderRoot
$venv = Join-Path $root ".venv"
$python = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $python)) {
    if ($PythonLauncher -eq "py") { & py -3.11 -m venv $venv } else { & $PythonLauncher -m venv $venv }
    if ($LASTEXITCODE -ne 0) { throw "Could not create the Integrated Controller Python environment. Python 3.11 is required." }
}
& $python -m pip install --upgrade pip setuptools wheel
if ($LASTEXITCODE -ne 0) { throw "Could not update packaging tools." }
& $python -m pip install -e (Join-Path $root "python")
if ($LASTEXITCODE -ne 0) { throw "Integrated Controller package installation failed." }
$config = Join-Path $root "config"
$controllerConfig = Join-Path $config "controller.json"
New-Item -ItemType Directory -Force -Path $config | Out-Null
if (-not (Test-Path $controllerConfig)) {
    Copy-Item (Join-Path $root "config_templates\controller.default.json") $controllerConfig
}
Write-Host "Arm Integrated Controller environment ready: $venv"
Write-Host "Local controller configuration: $controllerConfig"
