param([string]$PythonLauncher = "py")

. (Join-Path $PSScriptRoot "common.ps1")
$agent = Get-AgentRoot
$workspace = Get-WorkspaceRoot
$configDir = Join-Path $workspace "config"
New-Item -ItemType Directory -Force -Path $configDir | Out-Null

$python = Join-Path $workspace ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    if ($PythonLauncher -eq "py") {
        & py -3.11 -m venv (Join-Path $workspace ".venv")
    }
    else {
        & $PythonLauncher -m venv (Join-Path $workspace ".venv")
    }
}
& $python -m pip install --upgrade pip

$providerPython = Join-Path $workspace "providers\orbbec_femto_bolt\python"
if (Test-Path (Join-Path $providerPython "pyproject.toml")) {
    & $python -m pip install -e $providerPython
    if ($LASTEXITCODE -ne 0) { throw "Provider support package installation failed." }
}
else {
    Write-Host "Orbbec provider package is not present; RGB/depth capture will be unavailable." -ForegroundColor Yellow
}

$spatialSkill = Join-Path $workspace "skills\spatial_registration_rgbd"
if (Test-Path (Join-Path $spatialSkill "pyproject.toml")) {
    & $python -m pip install -e $spatialSkill
    if ($LASTEXITCODE -ne 0) { throw "Spatial RGB-D Skill installation failed." }
}

$stationarySkill = Join-Path $workspace "skills\stationary_world_arm_alignment"
if (Test-Path (Join-Path $stationarySkill "pyproject.toml")) {
    & $python -m pip install -e $stationarySkill
    if ($LASTEXITCODE -ne 0) { throw "Stationary alignment Skill installation failed." }
}

$toolRegistrationSkill = Join-Path $workspace "skills\register_tool_to_control_frame"
if (Test-Path (Join-Path $toolRegistrationSkill "pyproject.toml")) {
    & $python -m pip install -e $toolRegistrationSkill
    if ($LASTEXITCODE -ne 0) { throw "Tool registration Skill installation failed." }
}

$effectorFrontSkill = Join-Path $workspace "skills\locate-effector-front"
if (Test-Path (Join-Path $effectorFrontSkill "pyproject.toml")) {
    & $python -m pip install -e $effectorFrontSkill
    if ($LASTEXITCODE -ne 0) { throw "Effector-front Skill installation failed." }
}

& $python -m pip install -e (Join-Path $agent "python")
if ($LASTEXITCODE -ne 0) { throw "Test-agent package installation failed." }

$keyFile = Join-Path $configDir "api_keys.env"
if (-not (Test-Path $keyFile)) {
    $keyTemplate = Join-Path $configDir "api_keys.env.example"
    if (-not (Test-Path -LiteralPath $keyTemplate)) {
        $keyTemplate = Join-Path $agent "config_templates\api_keys.env.example"
    }
    Copy-Item -LiteralPath $keyTemplate -Destination $keyFile
    Write-Host "Created $keyFile"
}
else {
    Write-Host "Kept existing $keyFile"
}
$systemFile = Join-Path $configDir "system.env"
if (-not (Test-Path $systemFile)) {
    $systemTemplate = Join-Path $configDir "system.env.example"
    if (-not (Test-Path -LiteralPath $systemTemplate)) {
        $systemTemplate = Join-Path $agent "config_templates\system.env.example"
    }
    Copy-Item -LiteralPath $systemTemplate -Destination $systemFile
}
Write-Host "Test-agent setup complete."
