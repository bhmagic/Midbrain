. (Join-Path $PSScriptRoot "common.ps1")
$root = Get-ProviderRoot
$py = Get-ProviderPython

$PreviousPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = Join-Path $root "python"
    & $py -m rebot_arm_integrated.config_repair --root $root
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
finally {
    $env:PYTHONPATH = $PreviousPythonPath
}

& $py -m compileall -q (Join-Path $root "python") (Join-Path $root "provider.py")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Push-Location (Join-Path $root "python")
try {
    & $py -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
finally {
    Pop-Location
}

Get-ChildItem (Join-Path $root "config") -Filter *.json | ForEach-Object {
    $Raw = [System.IO.File]::ReadAllText($_.FullName, [System.Text.UTF8Encoding]::new($false))
    if ([string]::IsNullOrWhiteSpace($Raw)) { throw "Empty JSON file: $($_.FullName)" }
    $Raw | ConvertFrom-Json | Out-Null
}

if (Get-Command node -ErrorAction SilentlyContinue) {
    & node --check (Join-Path $root "python\rebot_arm_integrated\web\app.js")
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

Write-Host "Arm Integrated staged trajectory controller verification passed."
