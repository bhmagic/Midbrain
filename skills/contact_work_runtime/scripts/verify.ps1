$skillRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$workspaceRoot = (Resolve-Path (Join-Path $skillRoot "..\..")).Path
$python = Join-Path $skillRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "Run scripts/setup.ps1 first." }
$PreviousPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = @(
        (Join-Path $skillRoot "python")
    ) -join ";"
    & $python -m compileall -q $skillRoot
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $python -m unittest discover -s (Join-Path $skillRoot "python\tests") -v
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
finally {
    $env:PYTHONPATH = $PreviousPythonPath
}
Write-Host "Contact Work Skill runtime verification passed."
