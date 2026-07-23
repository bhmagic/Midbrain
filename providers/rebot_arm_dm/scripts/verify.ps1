. (Join-Path $PSScriptRoot "common.ps1")
$provider = Get-ProviderRoot
$python = Get-PythonPath
if (-not (Test-Path $python)) { throw "Python environment is missing. Run scripts\setup.ps1 first." }
& $python -m compileall -q (Join-Path $provider "provider.py") (Join-Path $provider "python\rebot_arm_dm_provider")
if ($LASTEXITCODE -ne 0) { throw "Python compilation failed." }
$env:REBOT_PROVIDER_ROOT = $provider
& $python -m unittest discover -s (Join-Path $provider "python\tests") -v
if ($LASTEXITCODE -ne 0) { throw "Tests failed." }
Write-Host "Basic Controller verification passed."
