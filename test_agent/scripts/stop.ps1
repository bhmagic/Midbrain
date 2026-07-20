param([switch]$Quiet)
. (Join-Path $PSScriptRoot "common.ps1")
$agent = Get-AgentRoot
$file = Join-Path $agent "run\pid.json"
if (Test-Path $file) {
    $data=Get-Content $file | ConvertFrom-Json
    Stop-Process -Id $data.ui -Force -ErrorAction SilentlyContinue
    Remove-Item $file -Force -ErrorAction SilentlyContinue
}
if (-not $Quiet) { Write-Host "Test-agent UI stopped." }
