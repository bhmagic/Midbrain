. (Join-Path $PSScriptRoot "common.ps1")

function Show-JsonEndpoint {
    param([string]$Label, [string]$Url)
    Write-Host "`n$Label"
    try {
        Invoke-RestMethod -Uri $Url -TimeoutSec 5 | ConvertTo-Json -Depth 15
    }
    catch {
        Write-Host "Unavailable: $($_.Exception.Message)" -ForegroundColor Yellow
    }
}

Show-JsonEndpoint "Manager providers" "http://127.0.0.1:7001/v1/providers"
Show-JsonEndpoint "Manager capabilities" "http://127.0.0.1:7001/v1/capabilities"
Show-JsonEndpoint "Fabric" "http://127.0.0.1:7002/health"
Show-JsonEndpoint "Fabric streams" "http://127.0.0.1:7002/v1/streams"
Show-JsonEndpoint "Fabric snapshot" "http://127.0.0.1:7002/v1/snapshot"
Show-JsonEndpoint "Test agent" "http://127.0.0.1:8000/api/status"
