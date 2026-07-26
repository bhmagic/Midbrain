param(
    [int]$Port = 8111,
    [double]$CaptureSeconds = 2.0,
    [string]$MappingName = "",
    [switch]$NoBrowser
)

. (Join-Path $PSScriptRoot "common.ps1")
$workspace = Get-WorkspaceRoot
$python = Join-Path $workspace ".venv\Scripts\python.exe"

if (-not $MappingName) {
    $systemEnv = Join-Path $workspace "config\system.env"
    if (Test-Path -LiteralPath $systemEnv) {
        foreach ($line in Get-Content -LiteralPath $systemEnv) {
            $trimmed = $line.Trim()
            if (-not $trimmed -or $trimmed.StartsWith("#")) { continue }
            $parts = $trimmed.Split("=", 2)
            if ($parts.Count -eq 2 -and $parts[0].Trim() -eq "CAMERA_MAPPING_NAME") {
                $MappingName = $parts[1]
                break
            }
        }
    }
}
if (-not $MappingName) {
    $MappingName = "Local\FemtoBoltPipeline_CameraHost_v2"
}

if (-not (Test-Path $python)) {
    throw "Workspace Python environment is missing. Run providers\orbbec_femto_bolt\scripts\setup.ps1 first."
}

try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:7101/health" -TimeoutSec 2
    if ($health.residency -ne "HOT") {
        throw "Camera Provider is not HOT. Start the workspace before calibration."
    }
}
catch {
    throw "Camera Provider is unavailable. Run platform_core\scripts\run_workspace.ps1 first. $($_.Exception.Message)"
}

$arguments = @(
    "-m", "orbbec_femto_provider.calibration_gui",
    "--workspace-root", $workspace,
    "--mapping-name", $MappingName,
    "--port", $Port,
    "--capture-seconds", $CaptureSeconds
)
if ($NoBrowser) {
    $arguments += "--no-browser"
}

& $python @arguments
if ($LASTEXITCODE -ne 0) {
    throw "Accelerometer calibration GUI exited with code $LASTEXITCODE."
}
