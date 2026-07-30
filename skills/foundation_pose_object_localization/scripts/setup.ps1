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

$FoundationPosePython = (
    Resolve-Path (Join-Path $WorkspaceRoot "providers\foundation_pose\python")
).Path
Invoke-Checked $VenvPython @(
    "-m",
    "pip",
    "install",
    "-e",
    $FoundationPosePython,
    "-e",
    $SkillRoot
)

Write-Host "FoundationPose Skill environment ready: $VenvPython"
