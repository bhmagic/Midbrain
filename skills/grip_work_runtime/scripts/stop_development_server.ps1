param(
    [Parameter(Mandatory = $true)][int]$Port,
    [Parameter(Mandatory = $true)][string]$SkillKind,
    [switch]$Quiet
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Get-DevelopmentListenerProcessIds {
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

$listenerProcessIds = @(Get-DevelopmentListenerProcessIds -ListenerPort $Port)
if ($listenerProcessIds.Count -eq 0) {
    if (-not $Quiet) {
        Write-Host "$SkillKind developer surface is already stopped."
    }
    exit 0
}

$identityVerified = $false
try {
    $state = Invoke-RestMethod `
        -Uri "http://127.0.0.1:$Port/api/development" `
        -TimeoutSec 2
    $serviceProperty = $state.PSObject.Properties["service"]
    $serviceIdentity = if ($null -eq $serviceProperty) {
        ""
    }
    else {
        [string]$serviceProperty.Value
    }
    $identityVerified = (
        [string]$state.skill_kind -eq $SkillKind -and
        $serviceIdentity -in @("", "midbrain-grip-skill-development")
    )
}
catch {
    $identityVerified = $false
}

# The first scrap-grip prototype predates the structured development identity.
# Recognize its exact local page so an upgrade can remove only that stale server.
if (-not $identityVerified -and $SkillKind -eq "scrap_grip") {
    try {
        $legacyPage = Invoke-WebRequest `
            -UseBasicParsing `
            -Uri "http://127.0.0.1:$Port/" `
            -TimeoutSec 2
        $identityVerified = (
            $legacyPage.Content.Contains("<title>Grip Skill Development</title>") -and
            $legacyPage.Content.Contains("/api/profiles") -and
            $legacyPage.Content.Contains("/api/provider")
        )
    }
    catch {
        $identityVerified = $false
    }
}

if (-not $identityVerified) {
    throw (
        "TCP port $Port is not the expected $SkillKind developer service. " +
        "Refusing to stop listener PIDs $($listenerProcessIds -join ', ')."
    )
}

foreach ($processId in $listenerProcessIds) {
    Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
}

$deadline = [DateTime]::UtcNow.AddSeconds(5)
do {
    Start-Sleep -Milliseconds 100
    $remaining = @(Get-DevelopmentListenerProcessIds -ListenerPort $Port)
} while ($remaining.Count -gt 0 -and [DateTime]::UtcNow -lt $deadline)

if ($remaining.Count -gt 0) {
    throw (
        "$SkillKind developer listener PIDs $($remaining -join ', ') remain " +
        "on TCP port $Port after stop."
    )
}
if (-not $Quiet) {
    Write-Host "$SkillKind developer surface stopped."
}
