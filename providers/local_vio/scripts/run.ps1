. (Join-Path $PSScriptRoot "common.ps1")
$provider = Get-ProviderRoot
$workspace = Get-WorkspaceRoot
$python = Join-Path $workspace ".venv\Scripts\python.exe"
& $python (Join-Path $provider "provider.py") --manager-url http://127.0.0.1:7001 --fabric-url http://127.0.0.1:7002 --control-port 7102
