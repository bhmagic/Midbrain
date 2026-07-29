param(
    [string]$PythonLauncher = "py"
)

$ErrorActionPreference = "Stop"
$SkillRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$WorkspaceRoot = (Resolve-Path (Join-Path $SkillRoot "..\..")).Path
$Venv = Join-Path $SkillRoot ".venv"
$VenvPython = Join-Path $Venv "Scripts\python.exe"
$SpatialSkill = (
    Resolve-Path (Join-Path $WorkspaceRoot "skills\spatial_registration_rgbd")
).Path

function Invoke-Checked {
    param(
        [string]$Executable,
        [string[]]$Arguments
    )
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Executable exited with code $LASTEXITCODE"
    }
}

if (-not (Test-Path -LiteralPath $VenvPython)) {
    if ($PythonLauncher -eq "py") {
        Invoke-Checked "py" @("-3.11", "-m", "venv", $Venv)
    }
    else {
        Invoke-Checked $PythonLauncher @("-m", "venv", $Venv)
    }
}

Invoke-Checked $VenvPython @("-m", "pip", "install", "--upgrade", "pip")
Invoke-Checked $VenvPython @(
    "-m",
    "pip",
    "install",
    "pytest>=8,<10",
    "-e",
    $SpatialSkill,
    "-e",
    $SkillRoot
)

Write-Host "Effector-front Skill environment ready: $Venv"
