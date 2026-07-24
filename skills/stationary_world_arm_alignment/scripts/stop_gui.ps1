param([switch]$Quiet)

$ErrorActionPreference = "Stop"
$SkillRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$PidFile = Join-Path $SkillRoot "run\gui.pid.json"
if (-not (Test-Path -LiteralPath $PidFile)) {
    if (-not $Quiet) { Write-Host "Skill GUI is not recorded as running." }
    exit 0
}

$record = Get-Content -LiteralPath $PidFile | ConvertFrom-Json
$process = Get-Process -Id ([int]$record.gui) -ErrorAction SilentlyContinue
if ($null -ne $process) {
    Stop-Process -Id $process.Id -Force
}
Remove-Item -LiteralPath $PidFile -Force
if (-not $Quiet) { Write-Host "Skill GUI stopped." }
