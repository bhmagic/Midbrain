param(
    [switch]$NoBrowser,
    [switch]$NoCoreStart
)

$ErrorActionPreference = "Stop"
$SkillRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$WorkspaceRoot = (Resolve-Path (Join-Path $SkillRoot "..\..")).Path
$VenvPython = Join-Path $SkillRoot ".venv\Scripts\python.exe"
$CoreLauncher = Join-Path $WorkspaceRoot "platform_core\scripts\run_workspace.ps1"
$GuiUrl = "http://127.0.0.1:8011"

# PowerShell Start-Process rejects a child environment containing both Path and PATH.
$processEnvironment = [Environment]::GetEnvironmentVariables(
    [EnvironmentVariableTarget]::Process
)
$pathKeys = @($processEnvironment.Keys | Where-Object { [string]$_ -ieq "PATH" })
if ($pathKeys.Count -gt 1 -and $pathKeys -contains "Path" -and $pathKeys -contains "PATH") {
    [Environment]::SetEnvironmentVariable(
        "PATH",
        $null,
        [EnvironmentVariableTarget]::Process
    )
}

function Test-LocalHealth {
    param([Parameter(Mandatory = $true)][string]$Url)
    try {
        $result = Invoke-RestMethod -Uri $Url -TimeoutSec 2
        return $result.status -eq "ok"
    }
    catch {
        return $false
    }
}

function Wait-LocalHealth {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [int]$TimeoutSeconds = 45
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        if (Test-LocalHealth -Url $Url) { return }
        Start-Sleep -Milliseconds 300
    } while ((Get-Date) -lt $deadline)
    throw "Timed out waiting for $Url"
}

if (-not (Test-Path -LiteralPath $VenvPython)) {
    throw "Skill environment is missing. Run scripts\setup.ps1 first."
}

$managerUp = Test-LocalHealth -Url "http://127.0.0.1:7001/health"
$fabricUp = Test-LocalHealth -Url "http://127.0.0.1:7002/health"
if (-not ($managerUp -and $fabricUp)) {
    if ($NoCoreStart) {
        throw "Midbrain Manager and Fabric must be running when -NoCoreStart is used."
    }
    if ($managerUp -xor $fabricUp) {
        throw "Midbrain core is only partially running. Use platform_core\scripts\stop_workspace.ps1, then retry this launcher."
    }
    if (-not (Test-Path -LiteralPath $CoreLauncher)) {
        throw "Midbrain core launcher is missing: $CoreLauncher"
    }
    Write-Host "Starting Midbrain Manager and Fabric..."
    & $CoreLauncher -CoreOnly -NoBrowser
}
else {
    Write-Host "Reusing the running Midbrain Manager and Fabric."
}

& (Join-Path $PSScriptRoot "stop_gui.ps1") -Quiet
$logsRoot = Join-Path $SkillRoot "logs"
$runRoot = Join-Path $SkillRoot "run"
New-Item -ItemType Directory -Force -Path $logsRoot, $runRoot | Out-Null
$env:PHYSICAL_AGENT_ROOT = $WorkspaceRoot
$process = Start-Process -FilePath $VenvPython `
    -ArgumentList "-m", "stationary_world_arm_alignment.app" `
    -WorkingDirectory $SkillRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $logsRoot "gui.out.log") `
    -RedirectStandardError (Join-Path $logsRoot "gui.err.log") `
    -PassThru

try {
    Wait-LocalHealth -Url "$GuiUrl/health"
}
catch {
    $stderr = Join-Path $logsRoot "gui.err.log"
    throw "Skill GUI failed to start. See $stderr. $($_.Exception.Message)"
}

Write-Host "Skill GUI: $GuiUrl"
Write-Host "GUI startup is passive; use Request providers for explicit acquisition."
Write-Host "Set MIDBRAIN_GUI_AUTO_BOOTSTRAP_PROVIDERS=true only for legacy automatic acquisition."
Write-Host "Logs: $logsRoot"
Write-Host "Stop GUI: skills\stationary_world_arm_alignment\scripts\stop_gui.ps1"
if (-not $NoBrowser) {
    Start-Process $GuiUrl
}
