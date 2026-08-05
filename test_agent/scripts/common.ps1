Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-AgentRoot {
    return (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}

function Get-WorkspaceRoot {
    return (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
}

function Import-EnvFile {
    param([string]$Path)
    if (-not (Test-Path $Path)) { return }
    foreach ($line in Get-Content $Path) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) { continue }
        $parts = $trimmed.Split("=", 2)
        if ($parts.Count -eq 2) {
            [Environment]::SetEnvironmentVariable($parts[0].Trim(), $parts[1], "Process")
        }
    }
}

function Get-TcpListenerProcessId {
    param([Parameter(Mandatory = $true)][int]$Port)
    $connection = Get-NetTCPConnection `
        -LocalPort $Port `
        -State Listen `
        -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -ne $connection) {
        return [int]$connection.OwningProcess
    }
    $netstat = Join-Path $env:SystemRoot "System32\netstat.exe"
    foreach ($line in & $netstat -ano -p tcp 2>$null) {
        $fields = @($line.Trim() -split "\s+")
        if (
            $fields.Count -ge 5 -and
            $fields[0] -eq "TCP" -and
            $fields[1] -match (":" + $Port + "$") -and
            $fields[3] -eq "LISTENING"
        ) {
            return [int]$fields[4]
        }
    }
    return $null
}

function Get-VerifiedAgentUiListenerProcessId {
    try {
        $health = Invoke-RestMethod `
            -Uri "http://127.0.0.1:8000/health" `
            -TimeoutSec 2
        if ([string]$health.service -ne "physical-agent-ui") {
            return $null
        }
        return Get-TcpListenerProcessId -Port 8000
    }
    catch {
        return $null
    }
}
