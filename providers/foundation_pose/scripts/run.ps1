param([int]$ControlPort = 7103)

$ErrorActionPreference = "Stop"
$providerRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$projectRoot = (Resolve-Path (Join-Path $providerRoot "..\..")).Path
$python = Join-Path $providerRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { throw "Run scripts/setup.ps1 first" }
$env:PHYSICAL_AGENT_ROOT = $projectRoot
& $python (Join-Path $providerRoot "provider.py") --config (Join-Path $providerRoot "config_templates\provider.default.json") --manager-url "http://127.0.0.1:7001" --fabric-url "http://127.0.0.1:7002" --control-port $ControlPort
exit $LASTEXITCODE
