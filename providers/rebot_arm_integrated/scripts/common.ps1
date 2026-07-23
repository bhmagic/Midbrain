function Get-ProviderRoot { return (Resolve-Path (Join-Path $PSScriptRoot "..")).Path }
function Get-WorkspaceRoot { return (Resolve-Path (Join-Path (Get-ProviderRoot) "..\..")).Path }
function Get-ProviderPython { $p=Join-Path (Get-ProviderRoot) ".venv\Scripts\python.exe"; if(-not(Test-Path $p)){throw "Run scripts\setup.ps1 first."}; return $p }
