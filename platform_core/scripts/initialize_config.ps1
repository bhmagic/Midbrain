. (Join-Path $PSScriptRoot "common.ps1")
$workspace = Get-WorkspaceRoot
$core = Get-CoreRoot
$config = Join-Path $workspace "config"
New-Item -ItemType Directory -Force -Path $config | Out-Null

$systemTarget = Join-Path $config "system.env"
if (-not (Test-Path $systemTarget)) {
    Copy-Item (Join-Path $core "config_templates\system.env.example") $systemTarget
    Write-Host "Created $systemTarget"
}
else {
    Write-Host "Kept existing $systemTarget"
}

$providersTarget = Join-Path $config "providers.json"
if (-not (Test-Path $providersTarget)) {
    Copy-Item (Join-Path $core "config_templates\providers.json.example") $providersTarget
    Write-Host "Created $providersTarget"
}
else {
    Write-Host "Kept existing $providersTarget"
}

Write-Host "The config directory is persistent and is not replaced by component packages."
