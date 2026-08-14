. (Join-Path $PSScriptRoot "common.ps1")
$providerRoot = Get-ProviderRoot
$python = Get-ProviderPython
& $python -m compileall -q (Join-Path $providerRoot "python") (Join-Path $providerRoot "provider.py")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Push-Location (Join-Path $providerRoot "python")
try {
    & $python -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
finally {
    Pop-Location
}
Get-ChildItem (Join-Path $providerRoot "config_templates") -Filter *.json | ForEach-Object {
    Get-Content $_.FullName -Raw | ConvertFrom-Json | Out-Null
}
Write-Host "Independent Contact Work Provider verification passed."
