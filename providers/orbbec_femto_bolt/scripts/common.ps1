Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-ProviderRoot {
    return (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}

function Get-WorkspaceRoot {
    return (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
}
