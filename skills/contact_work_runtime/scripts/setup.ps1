param([string]$PythonLauncher = "py")
$skillRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$venv = Join-Path $skillRoot ".venv"
$python = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $python)) {
    if ($PythonLauncher -eq "py") {
        & py -3.11 -m venv $venv
    }
    else {
        & $PythonLauncher -m venv $venv
    }
    if ($LASTEXITCODE -ne 0) { throw "Could not create the Contact Skill runtime environment." }
}
& $python -m pip install --upgrade pip setuptools wheel
if ($LASTEXITCODE -ne 0) { throw "Could not update packaging tools." }
& $python -m pip install -e $skillRoot
if ($LASTEXITCODE -ne 0) { throw "Contact Skill runtime installation failed." }
Write-Host "Contact Skill runtime environment ready: $venv"
