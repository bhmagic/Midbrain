param([string]$PythonLauncher = "python")
$ErrorActionPreference = "Stop"
$skillRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runtimeRoot = (Resolve-Path (Join-Path $skillRoot "..\grip_work_runtime")).Path
$venv = Join-Path $skillRoot ".venv"
$python = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $python)) {
    if ($PythonLauncher -eq "py") { & py -3.11 -m venv $venv } else { & $PythonLauncher -m venv $venv }
    if ($LASTEXITCODE -ne 0) { throw "Could not create Lay Flat Skill environment." }
}
& $python -m pip install --upgrade pip setuptools wheel
if ($LASTEXITCODE -ne 0) { throw "Could not update packaging tools." }
& $python -m pip install "pytest>=8,<10" -e $runtimeRoot -e $skillRoot
if ($LASTEXITCODE -ne 0) { throw "Lay Flat Skill installation failed." }
$config = Join-Path $skillRoot "config"
New-Item -ItemType Directory -Force -Path $config | Out-Null
$motionProfiles = Join-Path $config "motion_profiles.json"
if (-not (Test-Path $motionProfiles)) {
    Copy-Item (Join-Path $skillRoot "config_templates\motion_profiles.default.json") $motionProfiles
}
$vectorProfiles = Join-Path $config "gripper_vector_profiles.json"
if (-not (Test-Path $vectorProfiles)) {
    Copy-Item (Join-Path $skillRoot "config_templates\gripper_vector_profiles.default.json") $vectorProfiles
}
Write-Host "Lay Flat Skill environment ready: $venv"
Write-Host "Local lay-flat motion profiles: $motionProfiles"
Write-Host "Local lay-flat gripper vector profiles: $vectorProfiles"
