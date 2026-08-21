param([string]$PythonLauncher = "python")
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$venv = Join-Path $root ".venv"
$python = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $python)) {
    if ($PythonLauncher -eq "py") { & py -3.11 -m venv $venv } else { & $PythonLauncher -m venv $venv }
    if ($LASTEXITCODE -ne 0) { throw "Could not create Grip Work Runtime environment." }
}
& $python -m pip install --upgrade pip setuptools wheel
if ($LASTEXITCODE -ne 0) { throw "Could not update packaging tools." }
& $python -m pip install -e "$root[test]"
if ($LASTEXITCODE -ne 0) { throw "Grip Work Runtime installation failed." }
Write-Host "Grip Work Runtime environment ready: $venv"
