$ErrorActionPreference = "Stop"
$SkillRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$VenvPython = Join-Path $SkillRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $VenvPython)) {
    throw "Skill environment is missing. Run scripts\setup.ps1 first."
}

& $VenvPython -m compileall -q (Join-Path $SkillRoot "python")
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
& $VenvPython -m pytest -q (Join-Path $SkillRoot "python\tests")
exit $LASTEXITCODE
