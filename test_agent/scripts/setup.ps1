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

& $python -m pip install -e (Join-Path $agent "python")
if ($LASTEXITCODE -ne 0) { throw "Test-agent package installation failed." }

$keyFile = Join-Path $configDir "api_keys.env"
if (-not (Test-Path $keyFile)) {
    Copy-Item (Join-Path $agent "config_templates\api_keys.env.example") $keyFile
    Write-Host "Created $keyFile"
}
else {
    Write-Host "Kept existing $keyFile"
}
$systemFile = Join-Path $configDir "system.env"
if (-not (Test-Path $systemFile)) {
    Copy-Item (Join-Path $agent "config_templates\system.env.example") $systemFile
}
Write-Host "Test-agent setup complete."
