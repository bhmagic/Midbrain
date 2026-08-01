. (Join-Path $PSScriptRoot "common.ps1")
$workspace = Get-WorkspaceRoot
$core = Get-CoreRoot
Set-Location $workspace

& (Join-Path $PSScriptRoot "initialize_config.ps1")
& (Join-Path $PSScriptRoot "ensure_local_signing_secrets.ps1") | Out-Null

if (-not (Get-Command cargo -ErrorAction SilentlyContinue)) {
    throw "Rust is not installed. Install rustup and use the x86_64-pc-windows-msvc toolchain."
}
if (-not (Get-Command cl -ErrorAction SilentlyContinue)) {
    throw "MSVC cl.exe is unavailable. Use Developer PowerShell for Visual Studio 2022."
}

Write-Host "Building Resource Provider Manager and World State Fabric"
Push-Location $core
try {
    & cargo build --release
    if ($LASTEXITCODE -ne 0) { throw "cargo build failed" }
}
finally {
    Pop-Location
}

Write-Host "Core setup complete."
