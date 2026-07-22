param(
    [Parameter(Mandatory = $true)]
    [string]$ModelId,

    [Parameter(Mandatory = $true)]
    [string]$MaskPath,

    [int]$TimeoutSeconds = 240
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

. (Join-Path $PSScriptRoot "common.ps1")

$workspace = Get-WorkspaceRoot
$manager = "http://127.0.0.1:7001"
$fabric = "http://127.0.0.1:7002"
$providerControl = "http://127.0.0.1:7103"
$providerId = "perception.object_pose.foundation_pose"
$ports = @(7001, 7002, 7101, 7102, 7103)
$sessionId = [guid]::NewGuid().ToString()
$mainError = $null

$runWorkspace = Join-Path $workspace "platform_core\scripts\run_workspace.ps1"
$stopWorkspace = Join-Path $workspace "platform_core\scripts\stop_workspace.ps1"
$resolvedMask = (Resolve-Path -LiteralPath $MaskPath).Path

function Get-ListeningPorts {
    $listening = @()
    foreach ($port in $ports) {
        $listener = Get-NetTCPConnection `
            -LocalPort $port `
            -State Listen `
            -ErrorAction SilentlyContinue
        if ($null -ne $listener) {
            $listening += $port
        }
    }
    return @($listening)
}

function Stop-WorkspaceCleanly {
    if (Test-Path -LiteralPath $stopWorkspace -PathType Leaf) {
        try {
            & $stopWorkspace
        }
        catch {
            Write-Warning (
                "Workspace stop script reported: " +
                $_.Exception.Message
            )
        }
    }
    Start-Sleep -Milliseconds 750
}

function Assert-WorkspaceStopped {
    $listening = @(Get-ListeningPorts)
    if ($listening.Count -gt 0) {
        throw (
            "Midbrain/Provider ports are still listening: " +
            ($listening -join ", ")
        )
    }
    foreach ($port in $ports) {
        Write-Host "[STOPPED] $port"
    }
}

function Wait-JsonEndpoint {
    param(
        [string]$Uri,
        [int]$Timeout = 30
    )

    $deadline = (Get-Date).AddSeconds($Timeout)
    $lastError = $null

    while ((Get-Date) -lt $deadline) {
        try {
            return Invoke-RestMethod `
                -Uri $Uri `
                -Method Get `
                -TimeoutSec 3
        }
        catch {
            $lastError = $_
            Start-Sleep -Milliseconds 500
        }
    }

    throw (
        "Timed out waiting for $Uri. Last error: " +
        $lastError.Exception.Message
    )
}

function Invoke-ProviderRequest {
    param(
        [string]$Action,
        [hashtable]$Payload
    )

    $body = @{
        action = $Action
        payload = $Payload
        request_id = [guid]::NewGuid().ToString()
    } | ConvertTo-Json -Depth 20

    return Invoke-RestMethod `
        -Uri "$manager/v1/providers/$providerId/request" `
        -Method Post `
        -ContentType "application/json" `
        -Body $body `
        -TimeoutSec 30
}

try {
    Write-Host "============================================================"
    Write-Host "FoundationPose live Manager/Fabric ESTIMATE validation"
    Write-Host "============================================================"
    Write-Host "Model: $ModelId"
    Write-Host "Mask:  $resolvedMask"
    Write-Host ""

    Write-Host "Initial clean termination..."
    Stop-WorkspaceCleanly
    Assert-WorkspaceStopped

    Write-Host ""
    Write-Host "Starting Midbrain workspace..."
    Push-Location $workspace
    try {
        & $runWorkspace
    }
    finally {
        Pop-Location
    }

    $managerHealth = Wait-JsonEndpoint -Uri "$manager/health" -Timeout 30
    $fabricHealth = Wait-JsonEndpoint -Uri "$fabric/health" -Timeout 30
    Write-Host "[OK] Manager: $($managerHealth.status)"
    Write-Host "[OK] Fabric:  $($fabricHealth.status)"

    Write-Host ""
    Write-Host "Starting FoundationPose through Manager..."
    Invoke-RestMethod `
        -Uri "$manager/v1/providers/$providerId/start" `
        -Method Post `
        -TimeoutSec 30 |
        Out-Null

    $providerHealth = Wait-JsonEndpoint `
        -Uri "$providerControl/health" `
        -Timeout 45

    if ($providerHealth.residency -ne "HOT") {
        Invoke-RestMethod `
            -Uri "$manager/v1/providers/$providerId/hot" `
            -Method Post `
            -TimeoutSec 30 |
            Out-Null
        Start-Sleep -Milliseconds 500
        $providerHealth = Wait-JsonEndpoint `
            -Uri "$providerControl/health" `
            -Timeout 30
    }

    if ($providerHealth.residency -ne "HOT") {
        throw "FoundationPose did not reach HOT residency."
    }

    Write-Host (
        "[OK] FoundationPose residency=" +
        $providerHealth.residency +
        " health=" +
        $providerHealth.health +
        " ready=" +
        $providerHealth.ready
    )

    $registry = Invoke-ProviderRequest -Action "list_models" -Payload @{}
    $model = @($registry.models | Where-Object { $_.model_id -eq $ModelId })

    if ($model.Count -ne 1) {
        throw "Model was not returned exactly once by list_models: $ModelId"
    }

    $model = $model[0]
    Write-Host "[OK] role=$($model.role)"
    Write-Host "[OK] semantic_frame=$($model.semantic_frame)"
    Write-Host "[OK] child_frame=$($model.default_child_frame)"

    Write-Host ""
    Write-Host "Submitting exactly one ESTIMATE through Manager..."

    $accepted = Invoke-ProviderRequest `
        -Action "estimate" `
        -Payload @{
            session_id = $sessionId
            model_id = $ModelId
            target_id = $ModelId
            mask_path = $resolvedMask
            max_duration_s = [double]$TimeoutSeconds
            max_update_hz = 3.0
        }

    Write-Host "[ACCEPTED] state=$($accepted.state) session=$sessionId"

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $finalStatus = $null
    $lastState = ""

    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 750

        $status = Invoke-ProviderRequest `
            -Action "status" `
            -Payload @{ session_id = $sessionId }

        if ($status.state -ne $lastState) {
            Write-Host (
                "[SESSION] state=" +
                $status.state +
                " results=" +
                $status.result_count +
                " frame=" +
                $status.last_frame_number
            )
            if ($status.last_error) {
                Write-Host "[SESSION] error=$($status.last_error)"
            }
            $lastState = $status.state
        }

        if ($status.state -in @("COMPLETED", "FAILED", "EXPIRED", "STOPPED")) {
            $finalStatus = $status
            break
        }
    }

    if ($null -eq $finalStatus) {
        throw "ESTIMATE timed out after $TimeoutSeconds seconds."
    }

    if ($finalStatus.state -ne "COMPLETED" -or [int]$finalStatus.result_count -ne 1) {
        throw (
            "ESTIMATE did not complete with one result. state=" +
            $finalStatus.state +
            " results=" +
            $finalStatus.result_count +
            " error=" +
            $finalStatus.last_error
        )
    }

    Write-Host ""
    Write-Host "Waiting for matching Fabric pose + transform observations..."

    $pose = $null
    $transform = $null
    $fabricDeadline = (Get-Date).AddSeconds(15)

    while ((Get-Date) -lt $fabricDeadline) {
        try {
            $candidatePose = Invoke-RestMethod `
                -Uri "$fabric/v1/latest/perception.object.pose" `
                -Method Get `
                -TimeoutSec 5

            if ($candidatePose.data.tracking_session_id -eq $sessionId) {
                $pose = $candidatePose
            }
        }
        catch {
        }

        try {
            $candidateTransform = Invoke-RestMethod `
                -Uri "$fabric/v1/latest/transform.foundation_pose.object" `
                -Method Get `
                -TimeoutSec 5

            if ($candidateTransform.data.session_epoch -eq $sessionId) {
                $transform = $candidateTransform
            }
        }
        catch {
        }

        if ($null -ne $pose -and $null -ne $transform) {
            break
        }

        Start-Sleep -Milliseconds 250
    }

    if ($null -eq $pose) {
        throw "Matching perception.object.pose observation was not found in Fabric."
    }

    if ($null -eq $transform) {
        throw "Matching transform.foundation_pose.object observation was not found in Fabric."
    }

    if ($pose.observed_at_us -ne $transform.observed_at_us) {
        throw (
            "Pose and transform timestamps differ: " +
            $pose.observed_at_us +
            " vs " +
            $transform.observed_at_us
        )
    }

    if ($pose.data.source_observed_at_us -ne $pose.observed_at_us) {
        throw "Pose observation timestamp does not match its source camera timestamp."
    }

    if ($transform.data.source.model_id -ne $ModelId) {
        throw "Transform source model_id does not match requested model."
    }

    if ($pose.data.object_role -ne $model.role) {
        throw "Pose object_role does not match the model registry."
    }

    if ($transform.data.source.object_role -ne $model.role) {
        throw "Transform object_role does not match the model registry."
    }

    if ($pose.data.child_frame -ne $model.default_child_frame) {
        throw "Pose child_frame does not match the stable model child frame."
    }

    if ($transform.data.child_frame -ne $model.default_child_frame) {
        throw "Transform child_frame does not match the stable model child frame."
    }

    Write-Host ""
    Write-Host "LIVE VALIDATION CHECKPOINT REACHED"
    Write-Host "Manager lifecycle:              PASS"
    Write-Host "Manager request routing:        PASS"
    Write-Host "FoundationPose ESTIMATE:        PASS"
    Write-Host "Fabric object-pose publish:     PASS"
    Write-Host "Fabric transform publish:       PASS"
    Write-Host "Source timestamp preservation:  PASS"
    Write-Host "Stable reporter child frame:    PASS"
    Write-Host "Model role metadata:            $($model.role)"
    Write-Host "Source frame number:            $($pose.data.source_frame_number)"
    Write-Host "Inference latency ms:           $($pose.data.latency_ms)"
}
catch {
    $mainError = $_
    Write-Host ""
    Write-Host "[FAILED] $($_.Exception.Message)"
}
finally {
    Write-Host ""
    Write-Host "Final clean termination..."

    try {
        Invoke-ProviderRequest `
            -Action "stop" `
            -Payload @{
                session_id = $sessionId
                reason = "live validation cleanup"
            } |
            Out-Null
    }
    catch {
    }

    try {
        Invoke-RestMethod `
            -Uri "$manager/v1/providers/$providerId/stop" `
            -Method Post `
            -TimeoutSec 10 |
            Out-Null
    }
    catch {
    }

    Stop-WorkspaceCleanly
    Assert-WorkspaceStopped
    Write-Host "Workspace state: STOPPED"

    if ($null -ne $mainError) {
        throw $mainError
    }
}
