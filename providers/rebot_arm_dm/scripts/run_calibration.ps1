param(
    [string]$ProviderUrl = "http://127.0.0.1:8791",
    [int]$Port = 8792,
    [switch]$NoBrowser
)
. (Join-Path $PSScriptRoot "common.ps1")
$provider = Get-ProviderRoot
$python = Get-PythonPath
if (-not (Test-Path $python)) { throw "Python environment is missing. Run scripts\setup.ps1 first." }
try { Invoke-RestMethod -Uri "$ProviderUrl/health" -TimeoutSec 2 | Out-Null } catch { throw "The Basic Controller is unavailable at $ProviderUrl. Start it first." }
$args = @("-m", "rebot_arm_dm_provider.calibration_gui", "--provider-url", $ProviderUrl, "--collision-config", (Join-Path $provider "config\calibration_collision_model.json"), "--port", $Port)
if ($NoBrowser) { $args += "--no-browser" }
$stopScript = Join-Path $PSScriptRoot "stop_calibration.ps1"
& $stopScript -Quiet
$logsRoot = Join-Path $provider "logs"
$runRoot = Join-Path $provider "run"
$pidFile = Join-Path $runRoot "calibration_gui.pid.json"
New-Item -ItemType Directory -Force -Path $logsRoot, $runRoot | Out-Null
$process = Start-Process `
    -FilePath $python `
    -ArgumentList $args `
    -WorkingDirectory $provider `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $logsRoot "calibration_gui.out.log") `
    -RedirectStandardError (Join-Path $logsRoot "calibration_gui.err.log") `
    -PassThru
@{
    gui = $process.Id
    url = "http://127.0.0.1:$Port/"
} | ConvertTo-Json | Set-Content -LiteralPath $pidFile -Encoding UTF8

$deadline = (Get-Date).AddSeconds(30)
do {
    if ($process.HasExited) {
        throw "Calibration GUI exited with code $($process.ExitCode)."
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
    throw "Timed out waiting for the calibration GUI."
}
if (-not $NoBrowser) {
    Start-Process "http://127.0.0.1:$Port/"
}
