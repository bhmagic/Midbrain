param([switch]$Quiet)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")
$provider = Get-ProviderRoot
$pidFile = Join-Path $provider "run\calibration_gui.pid.json"
if (-not (Test-Path -LiteralPath $pidFile)) {
    if (-not $Quiet) { Write-Host "Hardware Development GUI is not recorded as running." }
    exit 0
}

$record = Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json
$process = Get-Process -Id ([int]$record.gui) -ErrorAction SilentlyContinue
if ($null -ne $process) {
    Stop-Process -Id $process.Id -Force
}
Remove-Item -LiteralPath $pidFile -Force
if (-not $Quiet) { Write-Host "Hardware Development GUI stopped." }
