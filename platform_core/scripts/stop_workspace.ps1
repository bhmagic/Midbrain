param([switch]$Quiet)

. (Join-Path $PSScriptRoot "common.ps1")
$workspace = Get-WorkspaceRoot
$core = Get-CoreRoot
$pidsFile = Join-Path $core "run\pids.json"

try {
    $providers = Invoke-RestMethod -Uri "http://127.0.0.1:7001/v1/providers" -TimeoutSec 3
    foreach ($provider in $providers) {
        $id = [string]$provider.config.id
        try {
            Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:7001/v1/providers/$id/stop" -TimeoutSec 10 | Out-Null
        }
        catch {
            if (-not $Quiet) { Write-Host "Could not gracefully stop provider $id" }
        }
    }
}
catch {
    if (-not $Quiet) { Write-Host "Manager was not reachable for graceful provider stop." }
}

if (Test-Path $pidsFile) {
    $pids = Get-Content $pidsFile | ConvertFrom-Json
    Stop-PidSafely $pids.ui
    Stop-PidSafely $pids.manager
    Stop-PidSafely $pids.fabric
    Remove-Item $pidsFile -Force -ErrorAction SilentlyContinue
}

Get-ChildItem -Path (Join-Path $workspace "providers") -Filter "cleanup.ps1" -Recurse -ErrorAction SilentlyContinue |
    ForEach-Object {
        try { & $_.FullName -Quiet } catch { }
    }

if (-not $Quiet) { Write-Host "Workspace processes stopped." }
