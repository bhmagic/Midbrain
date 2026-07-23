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
& $python @args
exit $LASTEXITCODE
