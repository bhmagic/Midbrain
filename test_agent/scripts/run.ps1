param([switch]$NoBrowser)

. (Join-Path $PSScriptRoot "common.ps1")
$agent = Get-AgentRoot
$workspace = Get-WorkspaceRoot
$python = Join-Path $workspace ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "Run test_agent\scripts\setup.ps1 first." }
$env:PHYSICAL_AGENT_ROOT = $workspace
Import-EnvFile (Join-Path $workspace "config\system.env")
Import-EnvFile (Join-Path $workspace "config\api_keys.env")
New-Item -ItemType Directory -Force -Path (Join-Path $agent "logs"), (Join-Path $agent "run") | Out-Null
& (Join-Path $PSScriptRoot "stop.ps1") -Quiet
$process = Start-Process -FilePath $python `
    -ArgumentList "-m", "physical_agent_test.app" `
    -WorkingDirectory $workspace `
    -RedirectStandardOutput (Join-Path $agent "logs\ui.out.log") `
    -RedirectStandardError (Join-Path $agent "logs\ui.err.log") `
    -PassThru
@{ ui = $process.Id } | ConvertTo-Json | Set-Content (Join-Path $agent "run\pid.json")
$deadline=(Get-Date).AddSeconds(30)
do {
    try { $health=Invoke-RestMethod "http://127.0.0.1:8000/health" -TimeoutSec 2; if ($health.status -eq "ok") { break } } catch { Start-Sleep -Milliseconds 300 }
} while ((Get-Date) -lt $deadline)
Write-Host "Test UI: http://127.0.0.1:8000"
if (-not $NoBrowser) { Start-Process "http://127.0.0.1:8000" }
