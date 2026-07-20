function Get-ProviderRoot {
    return (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}
function Get-WorkspaceRoot {
    if ($env:PHYSICAL_AGENT_ROOT) { return (Resolve-Path $env:PHYSICAL_AGENT_ROOT).Path }
    return (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
}
