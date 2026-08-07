$ErrorActionPreference = "Stop"
$SkillRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$VenvPython = Join-Path $SkillRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $VenvPython)) {
    throw "Translation-refinement Skill environment is missing. Run scripts\setup.ps1 first."
}

$env:PYTHONDONTWRITEBYTECODE = "1"
& $VenvPython -m pytest -q -p no:cacheprovider (Join-Path $SkillRoot "python\tests")
if ($LASTEXITCODE -ne 0) { throw "Translation-refinement tests failed." }
