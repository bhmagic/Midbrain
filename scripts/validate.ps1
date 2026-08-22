[CmdletBinding()]
param(
    [string]$PythonLauncher = "py",
    [switch]$SkipPython,
    [switch]$SkipRust,
    [switch]$BuildNativeCamera,
    [string]$OrbbecIncludeDir = "C:\Program Files\OrbbecSDK 2.8.6\include",
    [string]$OrbbecLibrary = "C:\Program Files\OrbbecSDK 2.8.6\lib\OrbbecSDK.lib",
    [string]$OrbbecBinDir = "C:\Program Files\OrbbecSDK 2.8.6\bin"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$workspace = Split-Path $PSScriptRoot -Parent
$validationRoot = Join-Path $workspace ".validation"
$wheelRoot = Join-Path $validationRoot "wheels"

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $false)][string[]]$Arguments = @(),
        [Parameter(Mandatory = $false)][string]$WorkingDirectory = $workspace
    )

    Push-Location $WorkingDirectory
    try {
        & $FilePath @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "Command failed with exit code ${LASTEXITCODE}: $FilePath $($Arguments -join ' ')"
        }
    }
    finally {
        Pop-Location
    }
}

function Get-ValidationPython {
    $venvPython = Join-Path $validationRoot "venv\Scripts\python.exe"
    if (Test-Path $venvPython) {
        return $venvPython
    }

    New-Item -ItemType Directory -Force -Path $validationRoot | Out-Null

    if ($PythonLauncher -eq "py") {
        if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
            throw "Python launcher 'py' is not available. Install Python 3.11 or pass -PythonLauncher."
        }
        Invoke-Checked -FilePath "py" -Arguments @("-3.11", "-m", "venv", (Join-Path $validationRoot "venv"))
    }
    else {
        if (-not (Get-Command $PythonLauncher -ErrorAction SilentlyContinue)) {
            throw "Python launcher is not available: $PythonLauncher"
        }
        Invoke-Checked -FilePath $PythonLauncher -Arguments @("-m", "venv", (Join-Path $validationRoot "venv"))
    }

    if (-not (Test-Path $venvPython)) {
        throw "Validation virtual environment was not created: $venvPython"
    }
    return $venvPython
}

Write-Host "Validating repository: $workspace"

Write-Host "[docs] Checking documentation integrity"
& (Join-Path $PSScriptRoot "check_docs.ps1")

Write-Host "[1/7] Checking clean configuration baselines"
& (Join-Path $PSScriptRoot "test_config_baselines.ps1")

Write-Host "[2/7] Checking Python component environment isolation"
& (Join-Path $PSScriptRoot "test_python_environment_isolation.ps1")

Write-Host "[3/7] Checking JSON files"
$jsonRelativePaths = @(
    & git -C $workspace ls-files --cached --others --exclude-standard -- "*.json"
)
if ($LASTEXITCODE -ne 0) {
    throw "Failed to enumerate repository-controlled JSON files."
}

$activeProvidersConfig = "config/providers.json"
if ((Test-Path (Join-Path $workspace $activeProvidersConfig)) -and
    $jsonRelativePaths -notcontains $activeProvidersConfig) {
    $jsonRelativePaths += $activeProvidersConfig
}

$jsonFiles = @(
    $jsonRelativePaths |
        Sort-Object -Unique |
        ForEach-Object {
            $candidate = Join-Path $workspace $_
            if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                Get-Item -LiteralPath $candidate
            }
        }
)
foreach ($jsonFile in $jsonFiles) {
    Get-Content -Raw $jsonFile.FullName | ConvertFrom-Json | Out-Null
}
Write-Host "JSON files parsed: $($jsonFiles.Count)"

if (-not $SkipPython) {
    Write-Host "[4/7] Running Python checks and tests"
    $python = Get-ValidationPython
    $pythonValidation = Join-Path $PSScriptRoot "run_python_validation.py"
    Invoke-Checked -FilePath $python -Arguments @($pythonValidation, "install")
    Invoke-Checked -FilePath $python -Arguments @($pythonValidation, "test")

    Write-Host "[5/7] Building Python wheels"
    Invoke-Checked -FilePath $python -Arguments @(
        $pythonValidation, "wheels", "--wheel-root", $wheelRoot
    )
}
else {
    Write-Host "[4/7] Python validation skipped"
    Write-Host "[5/7] Python wheel build skipped"
}

if (-not $SkipRust) {
    Write-Host "[6/7] Checking and compiling Rust workspace"
    if (-not (Get-Command cargo -ErrorAction SilentlyContinue)) {
        throw "Cargo is not available. Install the stable Rust toolchain or pass -SkipRust."
    }
    # Format first so platform-gated Rust code is normalized by the installed toolchain.
    # The strict follow-up check confirms that formatting completed successfully.
    Invoke-Checked -FilePath "cargo" -Arguments @("fmt", "--all") -WorkingDirectory (Join-Path $workspace "platform_core")
    Invoke-Checked -FilePath "cargo" -Arguments @("fmt", "--all", "--", "--check") -WorkingDirectory (Join-Path $workspace "platform_core")
    Invoke-Checked -FilePath "cargo" -Arguments @("test", "--workspace") -WorkingDirectory (Join-Path $workspace "platform_core")
    Invoke-Checked -FilePath "cargo" -Arguments @("build", "--workspace", "--release") -WorkingDirectory (Join-Path $workspace "platform_core")
}
else {
    Write-Host "[6/7] Rust validation skipped"
}

if ($BuildNativeCamera) {
    Write-Host "[7/7] Building native CameraHost"
    $buildScript = Join-Path $workspace "providers\orbbec_femto_bolt\scripts\build_native.ps1"
    & $buildScript `
        -OrbbecIncludeDir $OrbbecIncludeDir `
        -OrbbecLibrary $OrbbecLibrary `
        -OrbbecBinDir $OrbbecBinDir `
        -Clean
    if ($LASTEXITCODE -ne 0) {
        throw "Native CameraHost build failed."
    }
}
else {
    Write-Host "[7/7] Native CameraHost build not requested"
}

Write-Host "Refreshing source integrity manifests"
& (Join-Path $PSScriptRoot "update_manifests.ps1")

Write-Host "Validation completed successfully."
if (Test-Path $wheelRoot) {
    Write-Host "Python wheels: $wheelRoot"
}
