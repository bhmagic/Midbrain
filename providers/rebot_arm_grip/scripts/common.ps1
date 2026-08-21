function Get-ProviderRoot {
    return (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}

function Get-WorkspaceRoot {
    return (Resolve-Path (Join-Path (Get-ProviderRoot) "..\..")).Path
}

function Get-ProviderPython {
    $python = Join-Path (Get-ProviderRoot) ".venv\Scripts\python.exe"
    if (-not (Test-Path $python)) {
        throw "Grip Provider environment is missing. Run scripts/setup.ps1 first."
    }
    return $python
}
