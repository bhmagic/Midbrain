$skillRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $skillRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "Run scripts/setup.ps1 first." }
& (Join-Path $PSScriptRoot "stop_ui.ps1") -Quiet
& $python (Join-Path $skillRoot "python\lay_flat_skill\dev_ui.py") --skill-root $skillRoot
exit $LASTEXITCODE
