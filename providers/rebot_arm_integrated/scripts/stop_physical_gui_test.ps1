param(
    [string]$ProjectRoot = "",
    [string]$BasicUrl = "http://127.0.0.1:8791",
    [string]$IntegratedUrl = "http://127.0.0.1:8793",
    [switch]$StopCore
)

$ErrorActionPreference = "Stop"

if (-not $ProjectRoot) {
    $ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
}

function Test-Endpoint {
    param([string]$Url)
    try {
        Invoke-RestMethod -Uri $Url -TimeoutSec 1 | Out-Null
        return $true
    }
    catch {
        return $false
    }
}

$IntegratedWasReachable = Test-Endpoint "$IntegratedUrl/health"
if ($IntegratedWasReachable) {
    try {
        Invoke-RestMethod -Method Post -Uri "$IntegratedUrl/v1/control/warm" | Out-Null
        $BasicState = Invoke-RestMethod "$BasicUrl/v1/arm/state"
        if ($null -ne $BasicState.lease) {
            Write-Warning "Integrated WARM left the Basic lease active; continuing to authoritative Basic safe-home."
        }
    }
    catch {
        Write-Warning (
            "Integrated WARM could not confirm gravity-float; continuing to authoritative Basic safe-home: " +
            $_.Exception.Message
        )
    }
}

if (Test-Endpoint "$BasicUrl/health") {
    $SafeHome = Invoke-RestMethod -Method Post -Uri "$BasicUrl/v1/calibration/safe-home" -Body "{}" -ContentType "application/json" -TimeoutSec 35
    if (-not $SafeHome.success) {
        $State = Invoke-RestMethod "$BasicUrl/v1/arm/state"
        throw (
            "Basic safe-home did not complete. Provider remains powered; " +
            "state=$($State.provider_state), health=$($State.health), last_error=$($State.last_error)"
        )
    }
    Write-Host "Basic safe-home confirmed."

    if (Test-Endpoint "$IntegratedUrl/health") {
        try {
            Invoke-RestMethod -Method Post -Uri "$IntegratedUrl/v1/control/stop" | Out-Null
        }
        catch {
            Write-Warning "Integrated STOP response was interrupted after safe-home: $($_.Exception.Message)"
        }
        $Deadline = (Get-Date).AddSeconds(10)
        while ((Get-Date) -lt $Deadline -and (Test-Endpoint "$IntegratedUrl/health")) {
            Start-Sleep -Milliseconds 250
        }
    }

    $Response = Invoke-RestMethod -Method Post -Uri "$BasicUrl/v1/control/stop"
    $Response | ConvertTo-Json -Depth 10

    $Deadline = (Get-Date).AddSeconds(45)
    while ((Get-Date) -lt $Deadline -and (Test-Endpoint "$BasicUrl/health")) {
        Start-Sleep -Milliseconds 500
    }
    if (Test-Endpoint "$BasicUrl/health") {
        $State = Invoke-RestMethod "$BasicUrl/v1/arm/state"
        throw (
            "Basic safe-home was confirmed, but provider shutdown did not complete. " +
            "Do not stop Midbrain core; state=$($State.provider_state), health=$($State.health), last_error=$($State.last_error)"
        )
    }
}

if ($StopCore) {
    Set-Location $ProjectRoot
    & "$ProjectRoot\platform_core\scripts\stop_workspace.ps1"
}

Write-Host "Arm MIT bring-up GUI test shutdown complete."
