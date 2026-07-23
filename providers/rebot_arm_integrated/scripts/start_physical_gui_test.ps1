param(
    [string]$ProjectRoot = "C:\Projects\testing_physical_ai",
    [string]$ManagerUrl = "http://127.0.0.1:7001",
    [string]$FabricUrl = "http://127.0.0.1:7002",
    [string]$BasicUrl = "http://127.0.0.1:8791",
    [string]$IntegratedUrl = "http://127.0.0.1:8793",
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
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
    Interaction = $Integrated.interaction_mode
    IK = $Integrated.ik_mode
    KpMultiplier = $Integrated.runtime.kp_multiplier
    MaxCommitCm = [Math]::Round([double]$Integrated.target.maximum_commit_distance_m * 100, 1)
    WaypointRateHz = $Integrated.trajectory.send_rate_hz
    FabricInput = $Integrated.fabric_input.last_result
    Fault = $Integrated.fault_reason
    LastError = $Integrated.last_error
} | Format-List

if (-not $Ready) { throw "Integrated MIT bring-up controller is not ready." }
if (-not $NoBrowser) { Start-Process $IntegratedUrl }

Write-Host "MIT bring-up GUI ready at $IntegratedUrl"
Write-Host "Start with payload mass 0 unless the held tool mass and tool-frame COM are known."
Write-Host "First physical move: PRESS_MIT + POSITION_3DOF + ONE_SHOT + Kp multiplier 1.0 + about 5 mm target change + 3 s duration."
Write-Host "Physical testing uses GUI Engage + Xbox LB directly; no preview or A-button step is required."
Write-Host "Basic MIT support runs at 50 Hz; unchanged POS_VEL/POS_TOR motor endpoints refresh at 10 Hz."
Write-Host "CONTACT_WORK/POS_TOR requires POSE_6DOF, a fresh float baseline, and a JOINT_6, WRENCH_6, or ISOTROPIC_2 budget."
Write-Host "After that is smooth, test larger Kp gradually. J1-J3 clamp at effective Kp 500; the GUI shows every effective value."
Write-Host "Then test HOLD_LB at 0.10 s replan interval using small moving targets. Releasing LB must return to gravity-float."
Write-Host "TRANSIT_SPEED holds its POS_VEL endpoint after arrival; use Float/LT explicitly, or HOLD_LB release, to return to gravity-float."
Write-Host "Test POSE_6DOF only after the 3-DoF path is physically predictable."
Write-Host "Authoritative shutdown command:"
$StopScript = Join-Path $ProviderRoot "scripts\stop_physical_gui_test.ps1"
Write-Host ('powershell -ExecutionPolicy Bypass -File "{0}" -ProjectRoot "{1}" -StopCore' -f $StopScript, $ProjectRoot)
