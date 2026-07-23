param(
    [string]$ManagerUrl = "http://127.0.0.1:7001",
    [string]$FabricUrl = "http://127.0.0.1:7002",
    [string]$BasicUrl = "http://127.0.0.1:8791"
)
. (Join-Path $PSScriptRoot "common.ps1")
$root = Get-ProviderRoot
$python = Get-ProviderPython
& $python (Join-Path $root "provider.py") `
    --config (Join-Path $root "config\controller.json") `
    --basic-url $BasicUrl `
    --manager-url $ManagerUrl `
    --fabric-url $FabricUrl
exit $LASTEXITCODE
