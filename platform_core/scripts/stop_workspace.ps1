param(
    [switch]$Quiet,
    [switch]$UseManagerShutdownExecution,
    [switch]$UseLocalShutdownFallback,
    [ValidateRange(0, 5000)]
    [int]$DelayMilliseconds = 0
)

if ($DelayMilliseconds -gt 0) {
    Start-Sleep -Milliseconds $DelayMilliseconds
}

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

function Stop-AdvertisedDeveloperSurfaces {
    $workspacePath = (Resolve-Path -LiteralPath $workspace).Path
    $manifestPaths = @(
        Get-ChildItem -LiteralPath (Join-Path $workspace "providers") -Filter "manifest.json" -Recurse -File -ErrorAction SilentlyContinue
        Get-ChildItem -LiteralPath (Join-Path $workspace "skills") -Filter "manifest.json" -Recurse -File -ErrorAction SilentlyContinue
    )
    foreach ($manifestPath in $manifestPaths) {
        try {
            $manifest = Get-Content -LiteralPath $manifestPath.FullName -Raw | ConvertFrom-Json
            $uiProperty = $manifest.PSObject.Properties["ui"]
            if ($null -eq $uiProperty) { continue }
            $developerProperty = $uiProperty.Value.PSObject.Properties["developer"]
            if ($null -eq $developerProperty) { continue }
            $developer = $developerProperty.Value
            $stopProperty = $developer.PSObject.Properties["stop_command"]
            if ($null -eq $stopProperty) { continue }
            $relative = [string]$stopProperty.Value
            $relative = $relative -replace "^[.\\/]+", ""
            $stopScript = (Resolve-Path -LiteralPath (Join-Path $workspace $relative)).Path
            if (
                -not $stopScript.StartsWith(
                    $workspacePath,
                    [System.StringComparison]::OrdinalIgnoreCase
                ) -or
                [System.IO.Path]::GetExtension($stopScript) -ne ".ps1"
            ) {
                throw "Refusing developer stop command outside the workspace: $stopScript"
            }
            & $stopScript -Quiet
        }
        catch {
            if (-not $Quiet) {
                Write-Host "Could not stop developer surface from $($manifestPath.FullName): $($_.Exception.Message)"
            }
        }
    }
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

$managerSequenceComplete = $false
if ($UseManagerShutdownExecution -and $UseLocalShutdownFallback) {
    throw (
        "-UseManagerShutdownExecution and -UseLocalShutdownFallback are " +
        "mutually exclusive."
    )
}

Stop-AdvertisedDeveloperSurfaces

$managerExecutionRequested = [bool]$UseManagerShutdownExecution
$managerHealth = $null
if ($managerReachable -and -not $UseLocalShutdownFallback) {
    $managerHealth = Invoke-RestMethod `
        -Uri "http://127.0.0.1:7001/health" `
        -TimeoutSec 3
    if ([bool]$managerHealth.shutdown_execution_enabled) {
        $managerExecutionRequested = $true
    }
}
elseif ($UseManagerShutdownExecution -and -not $managerReachable) {
    throw "Manager shutdown execution was explicitly requested, but Manager is unavailable."
}

if ($managerReachable -and $managerExecutionRequested) {
    if ($null -eq $managerHealth) {
        $managerHealth = Invoke-RestMethod `
            -Uri "http://127.0.0.1:7001/health" `
            -TimeoutSec 3
    }
    if (-not [bool]$managerHealth.shutdown_execution_enabled) {
        throw (
            "Manager shutdown execution is disabled. " +
            "The local safety fallback remains available with " +
            "-UseLocalShutdownFallback."
        )
    }

    $requestId = [guid]::NewGuid().ToString()
    $planRequest = @{
        owner_id = "workspace-supervisor"
        reason = "ordered workspace shutdown"
        request_id = $requestId
    } | ConvertTo-Json
    $plan = Invoke-RestMethod `
        -Method Post `
        -Uri "http://127.0.0.1:7001/v1/shutdown/plan" `
        -ContentType "application/json" `
        -Body $planRequest `
        -TimeoutSec 5
    if (@($plan.blockers).Count -gt 0) {
        throw (
            "Manager shutdown plan is blocked. Core remains active. Details: " +
            (@($plan.blockers) -join "; ")
        )
    }

    $executionRequest = @{
        request_id = [guid]::NewGuid().ToString()
        confirmation = "EXECUTE_MANAGER_PROVIDER_SHUTDOWN"
    } | ConvertTo-Json
    $execution = Invoke-RestMethod `
        -Method Post `
        -Uri "http://127.0.0.1:7001/v1/shutdown/$($plan.shutdown_id)/execute" `
        -ContentType "application/json" `
        -Body $executionRequest `
        -TimeoutSec 5

    $providerTimeoutSeconds = 0
    foreach ($provider in $providers) {
        $providerTimeoutSeconds += Get-ProviderStopTimeoutSeconds $provider
        $safeStateTimeout = $provider.config.PSObject.Properties["safe_state_timeout_ms"]
        if ($null -ne $safeStateTimeout) {
            $providerTimeoutSeconds += [Math]::Ceiling(
                [Math]::Max(1000, [int]$safeStateTimeout.Value) / 1000.0
            )
        }
    }
    $executionDeadline = (Get-Date).AddSeconds(
        [Math]::Min(180, [Math]::Max(20, $providerTimeoutSeconds + 10))
    )
    do {
        $execution = Invoke-RestMethod `
            -Uri "http://127.0.0.1:7001/v1/shutdown/executions/$($execution.execution_id)" `
            -TimeoutSec 3
        if ($execution.state -in @("AWAITING_SUPERVISOR")) {
            $managerSequenceComplete = $true
            break
        }
        if (
            $execution.state -in @(
                "BLOCKED_SAFETY_SUPPORT_RETAINED",
                "PARTIAL_FAILURE_AWAITING_SUPERVISOR"
            )
        ) {
            throw (
                "Manager shutdown execution did not fully complete. " +
                "Core remains active. state=$($execution.state), failures=" +
                (@($execution.failures) -join "; ")
            )
        }
        Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $executionDeadline)

    if (-not $managerSequenceComplete) {
        throw (
            "Timed out waiting for Manager shutdown execution. " +
            "Core remains active; last state=$($execution.state)"
        )
    }
}

if ($managerReachable -and -not $managerSequenceComplete) {
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
