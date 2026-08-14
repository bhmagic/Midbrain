$ErrorActionPreference = "Stop"
$skillRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $skillRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Run scripts/setup.ps1 first."
}

& $python -m compileall -q (Join-Path $skillRoot "python")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $python -m pytest -q --import-mode=importlib (
    Join-Path $skillRoot "python\tests"
)
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Get-Content -LiteralPath (
    Join-Path $skillRoot "manifest.json"
) -Raw | ConvertFrom-Json | Out-Null
Get-Content -LiteralPath (
    Join-Path $skillRoot "config_templates\motion_profiles.default.json"
) -Raw | ConvertFrom-Json | Out-Null
Write-Host "Slicing Skill verification passed."
