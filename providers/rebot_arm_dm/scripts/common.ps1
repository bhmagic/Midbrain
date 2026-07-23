function Get-ProviderRoot {
    return (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}

function Get-WorkspaceRoot {
    $provider = Get-ProviderRoot
    $candidate = Resolve-Path (Join-Path $provider "..\..")
    if (Test-Path (Join-Path $candidate "platform_core")) { return $candidate.Path }
    return $provider
}

function Get-PythonPath {
    $provider = Get-ProviderRoot
    return (Join-Path $provider ".venv\Scripts\python.exe")
}
