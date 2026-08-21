param(
    [string]$ManagerUrl = "http://127.0.0.1:7001",
    [string]$FabricUrl = "http://127.0.0.1:7002",
    [string]$BasicUrl = "http://127.0.0.1:8791",
    [string]$ContactUrl = "http://127.0.0.1:8794"
)
. (Join-Path $PSScriptRoot "common.ps1")
$providerRoot = Get-ProviderRoot
$python = Get-ProviderPython
& $python (Join-Path $providerRoot "provider.py") `
    --config (Join-Path $providerRoot "config\controller.json") `
    --basic-url $BasicUrl `
    --contact-url $ContactUrl `
    --manager-url $ManagerUrl `
    --fabric-url $FabricUrl
exit $LASTEXITCODE
