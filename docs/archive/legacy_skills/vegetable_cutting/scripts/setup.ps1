param(
    [string]$PythonLauncher = "py"
)

$ErrorActionPreference = "Stop"
$SkillRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$WorkspaceRoot = (Resolve-Path (Join-Path $SkillRoot "..\..")).Path
$VenvPython = Join-Path $SkillRoot ".venv\Scripts\python.exe"

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
        Invoke-Checked "py" @("-3.11", "-m", "venv", (Join-Path $SkillRoot ".venv"))
    }
    else {
        Invoke-Checked $PythonLauncher @("-m", "venv", (Join-Path $SkillRoot ".venv"))
    }
}

Invoke-Checked $VenvPython @("-m", "pip", "install", "--upgrade", "pip")
$SpatialRegistrationRoot = (
    Resolve-Path (Join-Path $WorkspaceRoot "skills\spatial_registration_rgbd")
).Path
$OrbbecPython = (
    Resolve-Path (Join-Path $WorkspaceRoot "providers\orbbec_femto_bolt\python")
).Path
Invoke-Checked $VenvPython @(
    "-m",
    "pip",
    "install",
    "-e",
    $SpatialRegistrationRoot,
    "-e",
    $OrbbecPython,
    "-e",
    $SkillRoot
)

Write-Host "Vegetable cutting Skill environment ready: $VenvPython"
