$ErrorActionPreference = "Stop"
$SkillRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$VenvPython = Join-Path $SkillRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $VenvPython)) {
    throw "Skill environment is missing. Run scripts\setup.ps1 first."
}

$PythonRoot = Join-Path $SkillRoot "python"
$WorkspaceRoot = (Resolve-Path (Join-Path $SkillRoot "..\..")).Path
$SpatialPythonRoot = Join-Path $WorkspaceRoot "skills\spatial_registration_rgbd\python"
$PreviousPythonPath = $env:PYTHONPATH
$PythonPathParts = @(
    $PythonRoot,
    $SpatialPythonRoot,
    $PreviousPythonPath
) |
    Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) }
$env:PYTHONPATH = $PythonPathParts -join [IO.Path]::PathSeparator
try {
    & $VenvPython (Join-Path $PSScriptRoot "syntax_check.py") $PythonRoot
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
    $env:PYTHONDONTWRITEBYTECODE = "1"
    & $VenvPython -m pytest -q (Join-Path $SkillRoot "python\tests")
    exit $LASTEXITCODE
}
finally {
    $env:PYTHONPATH = $PreviousPythonPath
}
