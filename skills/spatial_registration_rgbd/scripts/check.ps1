$ErrorActionPreference = "Stop"
$SkillRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$VenvPython = Join-Path $SkillRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $VenvPython)) {
    throw "Spatial registration Skill environment is missing. Run scripts\setup.ps1 first."
}

& $VenvPython -m compileall -q (Join-Path $SkillRoot "python")
if ($LASTEXITCODE -ne 0) { throw "Spatial registration compilation failed." }

& $VenvPython -m pytest -q (Join-Path $SkillRoot "python\tests")
if ($LASTEXITCODE -ne 0) { throw "Spatial registration tests failed." }
