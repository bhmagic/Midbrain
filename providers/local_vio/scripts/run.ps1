. (Join-Path $PSScriptRoot "common.ps1")
$provider = Get-ProviderRoot
$python = Join-Path $provider ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Local VIO environment is missing. Run scripts\setup.ps1 first."
}
& $python (Join-Path $provider "provider.py") --manager-url http://127.0.0.1:7001 --fabric-url http://127.0.0.1:7002 --control-port 7102
