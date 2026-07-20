param(
    [switch]$NoBrowser,
    [switch]$CoreOnly
)

. (Join-Path $PSScriptRoot "common.ps1")
$workspace = Get-WorkspaceRoot
$core = Get-CoreRoot
Set-Location $workspace

& (Join-Path $PSScriptRoot "initialize_config.ps1") | Out-Null
Import-EnvFile (Join-Path $workspace "config\system.env")
Import-EnvFile (Join-Path $workspace "config\api_keys.env")

$managerExe = Join-Path $core "target\release\resource-provider-manager.exe"
$fabricExe = Join-Path $core "target\release\world-state-fabric.exe"
$providerConfig = Join-Path $workspace "config\providers.json"
$python = Join-Path $workspace ".venv\Scripts\python.exe"

foreach ($required in @($managerExe, $fabricExe, $providerConfig)) {
    if (-not (Test-Path $required)) {
        throw "Missing required file: $required. Run platform_core\scripts\setup_workspace.ps1 first."
    }
}

$env:PHYSICAL_AGENT_ROOT = $workspace
if (Test-Path $python) {
    $env:PHYSICAL_AGENT_PYTHON = $python
}
elseif (-not $CoreOnly) {
    throw "Shared Python environment is missing at $python. Run setup_workspace.ps1."
}

New-Item -ItemType Directory -Force -Path (Join-Path $core "logs"), (Join-Path $core "run") | Out-Null
& (Join-Path $PSScriptRoot "stop_workspace.ps1") -Quiet

$fabricProcess = Start-Process -FilePath $fabricExe `
    -WorkingDirectory $workspace `
    -RedirectStandardOutput (Join-Path $core "logs\fabric.out.log") `
    -RedirectStandardError (Join-Path $core "logs\fabric.err.log") `
    -PassThru
Wait-HttpHealth -Url "http://127.0.0.1:7002/health" | Out-Null

$managerProcess = Start-Process -FilePath $managerExe `
    -ArgumentList $providerConfig `
    -WorkingDirectory $workspace `
    -RedirectStandardOutput (Join-Path $core "logs\manager.out.log") `
    -RedirectStandardError (Join-Path $core "logs\manager.err.log") `
    -PassThru
Wait-HttpHealth -Url "http://127.0.0.1:7001/health" | Out-Null

$pids = @{
    fabric = $fabricProcess.Id
    manager = $managerProcess.Id
    ui = $null
}

$agentModule = Join-Path $workspace "test_agent\python\physical_agent_test\app.py"
if (-not $CoreOnly -and (Test-Path $agentModule)) {
    $uiProcess = Start-Process -FilePath $python `
        -ArgumentList "-m", "physical_agent_test.app" `
        -WorkingDirectory $workspace `
        -RedirectStandardOutput (Join-Path $core "logs\ui.out.log") `
        -RedirectStandardError (Join-Path $core "logs\ui.err.log") `
        -PassThru
    Wait-HttpHealth -Url "http://127.0.0.1:8000/health" -TimeoutSeconds 30 | Out-Null
    $pids.ui = $uiProcess.Id
}

$pids | ConvertTo-Json | Set-Content (Join-Path $core "run\pids.json")
Write-Host "Manager: http://127.0.0.1:7001"
Write-Host "Fabric:  http://127.0.0.1:7002"
if ($null -ne $pids.ui) {
    Write-Host "Test UI: http://127.0.0.1:8000"
    if (-not $NoBrowser) { Start-Process "http://127.0.0.1:8000" }
}
else {
    Write-Host "Test-agent UI was not started."
}
Write-Host "Logs: $core\logs"
Write-Host "Stop: platform_core\scripts\stop_workspace.ps1"
