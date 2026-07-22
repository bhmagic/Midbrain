param(
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

. (Join-Path $PSScriptRoot "common.ps1")

$provider = Get-ProviderRoot

if ([string]::IsNullOrWhiteSpace($Python)) {
    $providerPython = Join-Path $provider ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $providerPython -PathType Leaf) {
        $Python = $providerPython
        $PythonArgs = @()
    }
    else {
        $Python = "py"
        $PythonArgs = @("-3.11")
    }
}
else {
    $PythonArgs = @()
}

function Invoke-CheckedPython {
    param([string[]]$Arguments)

    & $Python @PythonArgs @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Python validation command failed with exit code $LASTEXITCODE."
    }
}

Write-Host "Compiling Provider Python sources..."
Invoke-CheckedPython -Arguments @(
    "-m", "compileall", "-q",
    (Join-Path $provider "provider.py"),
    (Join-Path $provider "python\foundation_pose_provider"),
    (Join-Path $provider "python\tests"),
    (Join-Path $provider "tools\cad_prepare")
)

Write-Host "Running Provider regression tests..."
Invoke-CheckedPython -Arguments @(
    "-m", "pytest", "-q",
    (Join-Path $provider "python\tests")
)

Write-Host "Running static publication checks..."
Invoke-CheckedPython -Arguments @(
    (Join-Path $provider "scripts\validate_publication.py"),
    "--allow-runtime",
    "--allow-generated"
)

Write-Host "Publication validation: PASS"
