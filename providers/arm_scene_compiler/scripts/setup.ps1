param([string]$PythonLauncher = "py")

. (Join-Path $PSScriptRoot "common.ps1")
$provider = Get-ProviderRoot
$workspace = Get-WorkspaceRoot
$venv = Join-Path $provider ".venv"
$python = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    if ($PythonLauncher -eq "py") {
        & py -3.11 -m venv $venv
        if ($LASTEXITCODE -ne 0) {
            $bootstrapCandidates = @(
                (Join-Path $workspace "providers\local_vio\.venv\Scripts\python.exe"),
                (Join-Path $workspace "providers\orbbec_femto_bolt\.venv\Scripts\python.exe"),
                (Join-Path $workspace "test_agent\.venv\Scripts\python.exe")
            )
            $bootstrap = $bootstrapCandidates |
                Where-Object { Test-Path -LiteralPath $_ } |
                Select-Object -First 1
            if ($bootstrap) {
                Write-Host "Windows py launcher unavailable; bootstrapping from $bootstrap" -ForegroundColor Yellow
                & $bootstrap -m venv $venv
            }
        }
    }
    else {
        & $PythonLauncher -m venv $venv
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Arm scene compiler virtual environment creation failed."
    }
}
& $python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) {
    throw "Arm scene compiler pip upgrade failed."
}
$cameraSupport = Join-Path $workspace "providers\orbbec_femto_bolt\python"
if (-not (Test-Path -LiteralPath (Join-Path $cameraSupport "pyproject.toml"))) {
    throw "Orbbec BufferRef support is required by the arm scene compiler."
}
& $python -m pip install -e $cameraSupport
if ($LASTEXITCODE -ne 0) {
    throw "Orbbec BufferRef support installation failed."
}
& $python -m pip install -e (Join-Path $provider "python")
if ($LASTEXITCODE -ne 0) {
    throw "Arm scene compiler package installation failed."
}
& (Join-Path $PSScriptRoot "register.ps1")
Write-Host "Arm scene compiler environment ready: $venv"
