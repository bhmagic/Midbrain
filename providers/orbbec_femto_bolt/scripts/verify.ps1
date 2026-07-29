param(
    [int]$WaitSeconds = 30
)

. (Join-Path $PSScriptRoot "common.ps1")
$provider = Get-ProviderRoot
$python = Join-Path $provider ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "Orbbec Provider environment is missing. Run setup.ps1." }

try {
    $null = Invoke-RestMethod -Uri "http://127.0.0.1:7002/health" -TimeoutSec 3
}
catch {
    throw "World State Fabric is not running. Run platform_core\scripts\run_workspace.ps1 before verification."
}

$output = Join-Path $provider "captures"
& $python -m orbbec_femto_provider.verify_capture --output-dir $output --wait-seconds $WaitSeconds
if ($LASTEXITCODE -ne 0) { throw "Camera pipeline verification failed." }
Start-Process (Join-Path $output "verify_rgb.jpg")
Start-Process (Join-Path $output "verify_depth.png")
Start-Process (Join-Path $output "verify_ir.png")
Start-Process (Join-Path $output "verify_depth_aligned_to_rgb.png")
