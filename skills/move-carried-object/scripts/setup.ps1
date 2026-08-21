param([string]$PythonLauncher = "python")
$ErrorActionPreference = "Stop"
$skillRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runtimeRoot = (Resolve-Path (Join-Path $skillRoot "..\grip_work_runtime")).Path
$venv = Join-Path $skillRoot ".venv"
$python = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $python)) {
    if ($PythonLauncher -eq "py") { & py -3.11 -m venv $venv } else { & $PythonLauncher -m venv $venv }
    if ($LASTEXITCODE -ne 0) { throw "Could not create Move Carried Object Skill environment." }
}
& $python -m pip install --upgrade pip setuptools wheel
if ($LASTEXITCODE -ne 0) { throw "Could not update packaging tools." }
& $python -m pip install "pytest>=8,<10" -e $runtimeRoot -e $skillRoot
if ($LASTEXITCODE -ne 0) { throw "Move Carried Object Skill installation failed." }
Write-Host "Move Carried Object Skill environment ready: $venv"
