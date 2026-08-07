param(
    [string]$PythonLauncher = "python"
)

$ErrorActionPreference = "Stop"
$SkillRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Venv = Join-Path $SkillRoot ".venv"
$VenvPython = Join-Path $Venv "Scripts\python.exe"

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
    "jsonschema>=4.23,<5",
    "-e",
    $SkillRoot
)

Write-Host "Arm-root translation refinement environment ready: $Venv"
