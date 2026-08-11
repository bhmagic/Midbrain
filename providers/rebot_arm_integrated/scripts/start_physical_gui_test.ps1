param(
    [string]$ProjectRoot = "",
    [string]$ManagerUrl = "http://127.0.0.1:7001",
    [string]$FabricUrl = "http://127.0.0.1:7002",
    [string]$BasicUrl = "http://127.0.0.1:8791",
    [string]$IntegratedUrl = "http://127.0.0.1:8793",
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
if (-not $ProjectRoot) {
    $ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
}
$ProviderRoot = Split-Path -Parent $PSScriptRoot
function Test-HttpService { param([string]$Url) try { Invoke-RestMethod -Uri $Url -TimeoutSec 1 | Out-Null; return $true } catch { return $false } }
function Wait-Json { param([string]$Url, [int]$TimeoutSeconds = 20) $Deadline = (Get-Date).AddSeconds($TimeoutSeconds); do { try { return Invoke-RestMethod -Uri $Url -TimeoutSec 2 } catch { Start-Sleep -Milliseconds 500 } } while ((Get-Date) -lt $Deadline); throw "Timed out waiting for $Url" }

$ManagerUp = Test-HttpService "$ManagerUrl/health"
$FabricUp = Test-HttpService "$FabricUrl/health"
if ($ManagerUp -xor $FabricUp) { throw "Manager and Fabric are not in the same state. Stop the partial workspace and retry." }
if (-not $ManagerUp) {
    Set-Location $ProjectRoot
    & "$ProjectRoot\platform_core\scripts\run_workspace.ps1" -CoreOnly -NoBrowser
    Wait-Json "$ManagerUrl/health" | Out-Null
    Wait-Json "$FabricUrl/health" | Out-Null
}
$Inhibit = Invoke-RestMethod "$ManagerUrl/v1/motion/inhibit"
if ($Inhibit.inhibited) { throw "Midbrain motion inhibit is active: $($Inhibit.owners | ConvertTo-Json -Depth 10 -Compress)" }

if (-not (Test-HttpService "$BasicUrl/health")) { Invoke-RestMethod -Method Post -Uri "$ManagerUrl/v1/providers/robot_arm.rebot_dm/start" | Out-Null }
$BasicHealth = Wait-Json "$BasicUrl/health"
$BasicState = Invoke-RestMethod "$BasicUrl/v1/arm/state"
if ($BasicHealth.simulation) { throw "Basic Controller is in simulation mode." }
if ($BasicState.provider_state -ne "SAFE_HOLD_GRAVITY_FLOAT") { throw "Basic is not in gravity-float: $($BasicState.provider_state)" }
if ($BasicState.health -in @("UNHEALTHY", "FAULTED")) { throw "Basic health is $($BasicState.health): $($BasicState.last_error)" }

if (-not (Test-HttpService "$IntegratedUrl/health")) { Invoke-RestMethod -Method Post -Uri "$ManagerUrl/v1/providers/robot_arm.primary.integrated/start" | Out-Null }
$Integrated = Wait-Json "$IntegratedUrl/health"
$Ready = $Integrated.ready -and $Integrated.basic_connected -and $Integrated.lease.active -and $Integrated.safety.float_confirmed -and $Integrated.safety.platform_ready -and -not $Integrated.safety.motion_inhibited

[pscustomobject]@{
    BasicState = $BasicState.provider_state
    BasicHealth = $BasicState.health
    IntegratedVersion = (Get-Content "$ProjectRoot\providers\rebot_arm_integrated\VERSION")
    IntegratedState = $Integrated.control_state
    IntegratedHealth = $Integrated.health
    Ready = $Ready
    MaxCommitCm = [Math]::Round([double]$Integrated.target.maximum_commit_distance_m * 100, 1)
    WaypointRateHz = $Integrated.trajectory.send_rate_hz
    SceneInput = $Integrated.scene_input.last_result
    Assembly = $Integrated.assembly.assembly_id
    AssemblyFingerprint = $Integrated.assembly_fingerprint
    Fault = $Integrated.fault_reason
    LastError = $Integrated.last_error
} | Format-List

if (-not $Ready) { throw "Integrated MIT bring-up controller is not ready." }
if (-not $NoBrowser) { Start-Process $IntegratedUrl }

Write-Host "Integrated read-only developer UI ready at $IntegratedUrl"
Write-Host "Run bounded physical qualification from the normal Agent UI through perform_relative_effector_motion."
Write-Host "Start with one 5 mm position-only move in verified open space, then inspect measured completion and the control audit."
Write-Host "Do not use this Provider for contact, gripping, manual target staging, gamepad teleoperation, or an undeclared held object."
Write-Host "General obstacle rerouting is not implemented; a blocked direct route may stop only at its closest-safe prefix."
Write-Host "Authoritative shutdown command:"
$StopScript = Join-Path $ProviderRoot "scripts\stop_physical_gui_test.ps1"
Write-Host ('powershell -ExecutionPolicy Bypass -File "{0}" -ProjectRoot "{1}" -StopCore' -f $StopScript, $ProjectRoot)
