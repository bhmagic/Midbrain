param(
    [int]$Port = 7114,
    [switch]$Quiet
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Get-LocateArmBaseListenerProcessIds {
    param([Parameter(Mandatory = $true)][int]$ListenerPort)

    $processIds = @(
        Get-NetTCPConnection `
            -State Listen `
            -LocalPort $ListenerPort `
            -ErrorAction SilentlyContinue |
            ForEach-Object { [int]$_.OwningProcess }
    )
    $netstat = Join-Path $env:SystemRoot "System32\netstat.exe"
    foreach ($line in & $netstat -ano -p tcp 2>$null) {
        $fields = @($line.Trim() -split "\s+")
        if (
            $fields.Count -ge 5 -and
            $fields[0] -eq "TCP" -and
            $fields[1] -match (":" + $ListenerPort + "$") -and
            $fields[3] -eq "LISTENING"
        ) {
            $processIds += [int]$fields[4]
        }
    }
    return @($processIds | Sort-Object -Unique)
}

$listenerProcessIds = @(
    Get-LocateArmBaseListenerProcessIds -ListenerPort $Port
)
if ($listenerProcessIds.Count -eq 0) {
    if (-not $Quiet) {
        Write-Host "Locate Arm Base developer surface is already stopped."
    }
    exit 0
}

$identityVerified = $false
try {
    $health = Invoke-RestMethod `
        -Uri "http://127.0.0.1:$Port/health" `
        -TimeoutSec 2
    $identityVerified = [string]$health.skill_id -eq "locate_arm_base"
}
catch {
    $identityVerified = $false
}
if (-not $identityVerified) {
    throw (
        "TCP port $Port is not the Locate Arm Base developer service. " +
        "Refusing to stop listener PIDs $($listenerProcessIds -join ', ')."
    )
}

$gracefulShutdownRequested = $false
try {
    $body = @{
        confirmation = "STOP_LOCATE_ARM_BASE_DEVELOPER_UI"
    } | ConvertTo-Json -Compress
    $response = Invoke-RestMethod `
        -Method Post `
        -Uri "http://127.0.0.1:$Port/v1/developer/shutdown" `
        -ContentType "application/json" `
        -Body $body `
        -TimeoutSec 2
    $gracefulShutdownRequested = (
        [string]$response.status -eq "SHUTDOWN_REQUESTED" -and
        [string]$response.skill_id -eq "locate_arm_base"
    )
}
catch {
    # An older installed developer server has no graceful shutdown route.
    $gracefulShutdownRequested = $false
}

if (-not $gracefulShutdownRequested) {
    foreach ($listenerProcessId in $listenerProcessIds) {
        Stop-Process `
            -Id $listenerProcessId `
            -Force `
            -ErrorAction SilentlyContinue
    }
}

$deadline = [DateTime]::UtcNow.AddSeconds(10)
do {
    Start-Sleep -Milliseconds 100
    $remaining = @(
        Get-LocateArmBaseListenerProcessIds -ListenerPort $Port
    )
} while ($remaining.Count -gt 0 -and [DateTime]::UtcNow -lt $deadline)

if ($remaining.Count -gt 0) {
    throw (
        "Locate Arm Base developer listener PIDs " +
        "$($remaining -join ', ') remain on TCP port $Port after stop."
    )
}
if (-not $Quiet) {
    Write-Host "Locate Arm Base developer surface stopped."
}
