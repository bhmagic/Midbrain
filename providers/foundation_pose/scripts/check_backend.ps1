$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

. (Join-Path $PSScriptRoot "common.ps1")

$provider = Get-ProviderRoot
$python = Join-Path $provider ".venv\Scripts\python.exe"
$foundationPoseRoot = Join-Path $provider "nvlabs\FoundationPose"

& $python -m foundation_pose_provider.smoke_test `
    --foundationpose-root $foundationPoseRoot

if ($LASTEXITCODE -ne 0) {
    throw "Native FoundationPose backend smoke test failed. See VALIDATION.md."
}
