Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-WorkspaceRoot {
    return (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
}

function Get-CoreRoot {
    return (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}

function Import-EnvFile {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path $Path)) { return }
    foreach ($line in Get-Content $Path) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) { continue }
        $parts = $trimmed.Split("=", 2)
        if ($parts.Count -ne 2) { continue }
        [Environment]::SetEnvironmentVariable($parts[0].Trim(), $parts[1], "Process")
    }
}

function Repair-DuplicateProcessPath {
    $processEnvironment = [Environment]::GetEnvironmentVariables("Process")
    $pathKeys = @(
        $processEnvironment.Keys |
            Where-Object { [string]$_ -ieq "Path" } |
            ForEach-Object { [string]$_ }
    )
    if ($pathKeys.Count -le 1) { return }

    $pathValue = [string]$processEnvironment[$pathKeys[0]]
    foreach ($pathKey in $pathKeys) {
        [Environment]::SetEnvironmentVariable($pathKey, $null, "Process")
    }
    [Environment]::SetEnvironmentVariable("Path", $pathValue, "Process")
}

function Wait-HttpHealth {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [int]$TimeoutSeconds = 30
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        try {
            $result = Invoke-RestMethod -Uri $Url -TimeoutSec 2
            if ($result.status -eq "ok") { return $result }
        }
        catch {
            Start-Sleep -Milliseconds 300
        }
    } while ((Get-Date) -lt $deadline)
    throw "Timed out waiting for $Url"
}

function Stop-PidSafely {
    param([Nullable[int]]$PidValue)
    if ($null -eq $PidValue) { return }
    $process = Get-Process -Id $PidValue -ErrorAction SilentlyContinue
    if ($null -ne $process) {
        Stop-Process -Id $PidValue -Force -ErrorAction SilentlyContinue
    }
}
