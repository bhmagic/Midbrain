param([string]$PythonLauncher = "py")

$ErrorActionPreference = "Stop"
$skillRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runtimeRoot = (Resolve-Path (Join-Path $skillRoot "..\contact_work_runtime")).Path
$venv = Join-Path $skillRoot ".venv"
$python = Join-Path $venv "Scripts\python.exe"

function Invoke-Checked {
    param([string]$Executable, [string[]]$Arguments)
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Executable exited with code $LASTEXITCODE"
    }
}

if (-not (Test-Path -LiteralPath $python)) {
    if ($PythonLauncher -eq "py") {
        Invoke-Checked "py" @("-3.11", "-m", "venv", $venv)
    }
    else {
        Invoke-Checked $PythonLauncher @("-m", "venv", $venv)
    }
}

Invoke-Checked $python @("-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel")
Invoke-Checked $python @(
    "-m", "pip", "install", "pytest>=8,<10", "-e", $runtimeRoot, "-e", $skillRoot
)

$configDirectory = Join-Path $skillRoot "config"
$motionProfiles = Join-Path $configDirectory "motion_profiles.json"
New-Item -ItemType Directory -Force -Path $configDirectory | Out-Null
if (-not (Test-Path -LiteralPath $motionProfiles)) {
    Copy-Item -LiteralPath (
        Join-Path $skillRoot "config_templates\motion_profiles.default.json"
    ) -Destination $motionProfiles
}

Write-Host "Slicing Skill environment ready: $venv"
Write-Host "Local slicing motion profiles: $motionProfiles"
