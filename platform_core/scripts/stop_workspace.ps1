param([switch]$Quiet)

. (Join-Path $PSScriptRoot "common.ps1")
$workspace = Get-WorkspaceRoot
$core = Get-CoreRoot
$pidsFile = Join-Path $core "run\pids.json"

function Test-SafetyCriticalProvider {
    param([string]$ProviderId)
    return $ProviderId -in @(
        "robot_arm.primary.integrated",
        "robot_arm.rebot_dm"
    )
}

function Get-ProviderStopPriority {
    param([string]$ProviderId)
    switch ($ProviderId) {
        "robot_arm.primary.integrated" { return 0 }
        "robot_arm.rebot_dm" { return 1 }
        default { return 50 }
    }
}

function Get-ProviderStopTimeoutSeconds {
    param($Provider)
    $timeoutMs = 10000
    $property = $Provider.config.PSObject.Properties["graceful_stop_timeout_ms"]
    if ($null -ne $property) {
        $timeoutMs = [Math]::Max(1000, [int]$property.Value)
    }
    return [int]([Math]::Ceiling($timeoutMs / 1000.0) + 5)
}

$managerReachable = $false
$providers = @()
try {
    $providerResponse = Invoke-RestMethod -Uri "http://127.0.0.1:7001/v1/providers" -TimeoutSec 3
    if ($null -eq $providerResponse.PSObject.Methods["GetEnumerator"]) {
        throw "Manager returned an invalid provider collection."
    }
    # Windows PowerShell can preserve a REST JSON array as one pipeline object.
    # Enumerate it explicitly so dependency sorting and safety checks see each provider.
    $providers = @($providerResponse.GetEnumerator())
    $providerIds = @(
        $providers | ForEach-Object { [string]$_.config.id }
    )
    foreach ($providerId in $providerIds) {
        if (
            [string]::IsNullOrWhiteSpace($providerId) -or
            $providerId -notmatch "^[A-Za-z0-9._-]+$"
        ) {
            throw "Manager returned an invalid provider identifier '$providerId'."
        }
    }
    if (@($providerIds | Select-Object -Unique).Count -ne $providerIds.Count) {
        throw "Manager returned duplicate provider identifiers."
    }
    $managerReachable = $true
}
catch {
    if (-not $Quiet) {
        Write-Host "Manager provider discovery failed: $($_.Exception.Message)"
    }
}

if ($managerReachable) {
    $orderedProviders = @(
        $providers | Sort-Object `
            @{ Expression = { Get-ProviderStopPriority ([string]$_.config.id) } }, `
            @{ Expression = { [string]$_.config.id } }
    )
    foreach ($provider in $orderedProviders) {
        $id = [string]$provider.config.id
        $timeoutSeconds = Get-ProviderStopTimeoutSeconds $provider
        try {
            $response = Invoke-RestMethod `
                -Method Post `
                -Uri "http://127.0.0.1:7001/v1/providers/$id/stop" `
                -TimeoutSec $timeoutSeconds
            if ((Test-SafetyCriticalProvider $id) -and [string]$response.status -ne "stopped") {
                throw "manager returned status '$($response.status)'"
            }
        }
        catch {
            if (Test-SafetyCriticalProvider $id) {
                throw (
                    "Safety-critical provider $id did not confirm a graceful stop. " +
                    "Midbrain core remains active so powered gravity support is not interrupted. " +
                    "Details: $($_.Exception.Message)"
                )
            }
            if (-not $Quiet) {
                Write-Host "Could not gracefully stop provider $id`: $($_.Exception.Message)"
            }
        }
    }
}
else {
    foreach ($armUrl in @("http://127.0.0.1:8793/health", "http://127.0.0.1:8791/health")) {
        try {
            Invoke-RestMethod -Uri $armUrl -TimeoutSec 1 | Out-Null
            throw (
                "Safety-critical arm provider is reachable at $armUrl while Manager is unavailable. " +
                "Refusing to force-stop the workspace."
            )
        }
        catch {
            if ($_.Exception.Message -like "Safety-critical arm provider*") {
                throw
            }
        }
    }
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
