param(
    [int]$ProviderPort = 8791,
    [int]$GuiPort = 8792
)
. (Join-Path $PSScriptRoot "common.ps1")
$provider = Get-ProviderRoot
$python = Get-PythonPath
if (-not (Test-Path $python)) { throw "Python environment is missing. Run scripts\setup.ps1 first." }
$providerArgs = @(
    (Join-Path $provider "provider.py"), "--simulate",
    "--config", (Join-Path $provider "config\arm_model.json"),
    "--calibration", (Join-Path $provider "config\arm_calibration.json"),
    "--listen-port", $ProviderPort
)
$process = Start-Process -FilePath $python -ArgumentList $providerArgs -PassThru
try {
    for ($i = 0; $i -lt 50; $i++) {
        try { Invoke-RestMethod -Uri "http://127.0.0.1:$ProviderPort/health" -TimeoutSec 1 | Out-Null; break } catch { Start-Sleep -Milliseconds 100 }
    }
    & (Join-Path $PSScriptRoot "run_calibration.ps1") -ProviderUrl "http://127.0.0.1:$ProviderPort" -Port $GuiPort
}
finally {
    try { Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$ProviderPort/v1/control/stop" -ContentType "application/json" -Body "{}" -TimeoutSec 3 | Out-Null } catch {}
    if (-not $process.HasExited) { $process.WaitForExit(8000) | Out-Null }
    if (-not $process.HasExited) { $process.Kill() }
}
