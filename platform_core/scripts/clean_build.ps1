. (Join-Path $PSScriptRoot "common.ps1")
$core = Get-CoreRoot
& cargo clean --manifest-path (Join-Path $core "Cargo.toml")
Write-Host "Core build outputs removed. Configuration was not changed."
