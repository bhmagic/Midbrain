param(
    [string]$BasicUrl = "http://127.0.0.1:8791",
    [string]$IntegratedUrl = "http://127.0.0.1:8793",
    [int]$PeriodMilliseconds = 200
)

$ErrorActionPreference = "Continue"
while ($true) {
    try {
        $Basic = Invoke-RestMethod -Uri "$BasicUrl/v1/arm/state" -TimeoutSec 1
        $Integrated = Invoke-RestMethod -Uri "$IntegratedUrl/v1/state" -TimeoutSec 1
        $Joint = $Integrated.joint_state
        $Trajectory = $Integrated.trajectory
        Clear-Host
        [pscustomobject]@{
            Time = [DateTime]::Now.ToString("HH:mm:ss.fff")
            BasicState = $Basic.provider_state
            BasicHealth = $Basic.health
            FeedbackAgeMs = [Math]::Round([double]$Basic.feedback_age_ms, 1)
            BasicLastTickMs = [Math]::Round([double]$Basic.loop.last_tick_duration_ms, 2)
            BasicLatenessMs = [Math]::Round([double]$Basic.loop.last_lateness_ms, 2)
            BasicMissedPeriods = $Basic.loop.missed_periods
            IntegratedState = $Integrated.control_state
            IntegratedHealth = $Integrated.health
            ControllerRole = $Integrated.controller_role
            Assembly = $Integrated.assembly.assembly_id
            AssemblyFingerprint = $Integrated.assembly_fingerprint
            FloatConfirmed = $Integrated.safety.float_confirmed
            SceneInput = $Integrated.scene_input.last_result
            SceneSequence = $Integrated.scene_input.last_sequence
            PlanId = $Integrated.planning.last_preview.preview_id
            TargetClamped = $Integrated.target.last_commit_clamped
            PositionResidualM = $Integrated.target.position_residual_m
            OrientationResidualRad = $Integrated.target.orientation_residual_rad
            TrajectoryActive = $Trajectory.active
            SegmentDurationS = $Trajectory.segment_duration_s
            Replans = $Integrated.live_replan_count
            FramesSent = $Trajectory.frames_sent
            FramesSkipped = $Trajectory.frames_skipped
            LastCommandLatencyMs = $Integrated.last_command_latency_ms
            MaxSendLatenessMs = $Trajectory.max_send_lateness_ms
            Fault = $Integrated.fault_reason
            LastError = $Integrated.last_error
        } | Format-List
        Write-Host "Joint measured / commanded / goal / speed / provider rate cap:"
        for ($Index = 0; $Index -lt 6; $Index++) {
            $Measured = [double]$Joint.measured_rad[$Index]
            $Commanded = if ($null -eq $Joint.commanded_rad) { [double]::NaN } else { [double]$Joint.commanded_rad[$Index] }
            $Goal = if ($null -eq $Joint.goal_rad) { [double]::NaN } else { [double]$Joint.goal_rad[$Index] }
            $Speed = [double]$Joint.velocity_rad_s[$Index]
            $Cap = [double]$Joint.provider_rate_caps_rad_s[$Index]
            Write-Host (("J{0}: measured={1,9:F4} commanded={2,9:F4} goal={3,9:F4} speed={4,8:F4} cap={5,7:F3}") -f ($Index + 1), $Measured, $Commanded, $Goal, $Speed, $Cap)
        }
    } catch { Clear-Host; Write-Warning $_.Exception.Message }
    Start-Sleep -Milliseconds ([Math]::Max(100, $PeriodMilliseconds))
}
