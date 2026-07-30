param(
    [int]$Port = 8111,
    [double]$CaptureSeconds = 2.0,
    [string]$MappingName = "",
    [switch]$NoBrowser
)

. (Join-Path $PSScriptRoot "common.ps1")
$provider = Get-ProviderRoot
$workspace = Get-WorkspaceRoot
$python = Join-Path $provider ".venv\Scripts\python.exe"

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
    throw "Orbbec Provider environment is missing. Run providers\orbbec_femto_bolt\scripts\setup.ps1 first."
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

$stopScript = Join-Path $PSScriptRoot "stop_accelerometer_calibration.ps1"
& $stopScript -Quiet
$logsRoot = Join-Path $provider "logs"
$runRoot = Join-Path $provider "run"
$pidFile = Join-Path $runRoot "accelerometer_calibration.pid.json"
New-Item -ItemType Directory -Force -Path $logsRoot, $runRoot | Out-Null
$process = Start-Process `
    -FilePath $python `
    -ArgumentList $arguments `
    -WorkingDirectory $provider `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $logsRoot "accelerometer_calibration.out.log") `
    -RedirectStandardError (Join-Path $logsRoot "accelerometer_calibration.err.log") `
    -PassThru
@{
    gui = $process.Id
    url = "http://127.0.0.1:$Port/"
} | ConvertTo-Json | Set-Content -LiteralPath $pidFile -Encoding UTF8

$deadline = (Get-Date).AddSeconds(30)
do {
    if ($process.HasExited) {
        throw "Accelerometer calibration GUI exited with code $($process.ExitCode)."
    }
    try {
        Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$Port/" -TimeoutSec 1 | Out-Null
        break
    }
    catch {
        Start-Sleep -Milliseconds 250
    }
} while ((Get-Date) -lt $deadline)
if ((Get-Date) -ge $deadline) {
    throw "Timed out waiting for the accelerometer calibration GUI."
}
if (-not $NoBrowser) {
    Start-Process "http://127.0.0.1:$Port/"
}
