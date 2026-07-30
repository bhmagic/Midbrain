param([switch]$Quiet)

$ErrorActionPreference = "Stop"
$providerRoot = Split-Path -Parent $PSScriptRoot
$pidFile = Join-Path $providerRoot "run\tracking_gui.pid.json"
if (-not (Test-Path -LiteralPath $pidFile)) {
    if (-not $Quiet) { Write-Host "FoundationPose tracking GUI is not recorded as running." }
    exit 0
}

$record = Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json
$process = Get-Process -Id ([int]$record.gui) -ErrorAction SilentlyContinue
if ($null -ne $process) {
    Stop-Process -Id $process.Id -Force
}
Remove-Item -LiteralPath $pidFile -Force
if (-not $Quiet) { Write-Host "FoundationPose tracking GUI stopped." }
