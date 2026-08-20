[CmdletBinding()]
param(
    [string]$PythonLauncher = "python",
    [string]$Sam2Source = "",
    [string]$Sam2Commit = "2b90b9f5ceec907a1c18123530e92e794ad901a4",
    [string]$TorchIndexUrl = "https://download.pytorch.org/whl/cu128"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$providerRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$workspaceRoot = (Resolve-Path (Join-Path $providerRoot "..\..")).Path
$venv = Join-Path $providerRoot ".venv"
$providerPython = Join-Path $venv "Scripts\python.exe"
if (-not $Sam2Source) {
    $Sam2Source = Join-Path $providerRoot "upstream\sam2"
}
if (-not (Test-Path -LiteralPath $Sam2Source -PathType Container)) {
    New-Item -ItemType Directory -Force -Path (Split-Path $Sam2Source -Parent) | Out-Null
    & git clone --filter=blob:none https://github.com/facebookresearch/sam2.git $Sam2Source
    if ($LASTEXITCODE -ne 0) { throw "Could not clone the pinned SAM2 source." }
}
$Sam2Source = (Resolve-Path -LiteralPath $Sam2Source).Path
if (-not (Test-Path -LiteralPath (Join-Path $Sam2Source ".git") -PathType Container)) {
    throw "SAM2 source is not a Git checkout: $Sam2Source"
}
& git -C $Sam2Source checkout --detach $Sam2Commit
if ($LASTEXITCODE -ne 0) { throw "Could not select pinned SAM2 commit $Sam2Commit." }
$actualSam2Commit = (& git -C $Sam2Source rev-parse HEAD).Trim()
if ($actualSam2Commit -ne $Sam2Commit) {
    throw "SAM2 source commit mismatch: expected $Sam2Commit, found $actualSam2Commit"
}

if (-not (Test-Path -LiteralPath $providerPython -PathType Leaf)) {
    & $PythonLauncher -m venv $venv
    if ($LASTEXITCODE -ne 0) {
        throw "Could not create the SAM2 scene tracker virtual environment."
    }
}

& $providerPython -m pip install --upgrade pip setuptools wheel
if ($LASTEXITCODE -ne 0) {
    throw "Could not update SAM2 tracker packaging tools."
}
& $providerPython -m pip install torch torchvision --index-url $TorchIndexUrl
if ($LASTEXITCODE -ne 0) {
    throw "Could not install the CUDA PyTorch runtime."
}
& $providerPython -m pip install hydra-core iopath pytest
if ($LASTEXITCODE -ne 0) {
    throw "Could not install SAM2 prerequisites."
}
& $providerPython -m pip install -e (Join-Path $workspaceRoot "contracts\python")
if ($LASTEXITCODE -ne 0) {
    throw "Could not install the provider-neutral BufferRef client."
}
& $providerPython -m pip install -e (Join-Path $providerRoot "python")
if ($LASTEXITCODE -ne 0) {
    throw "Could not install the Midbrain SAM2 tracker package."
}

$env:SAM2_BUILD_CUDA = "0"
try {
    & $providerPython -m pip install --no-deps --no-build-isolation -e $Sam2Source
    if ($LASTEXITCODE -ne 0) {
        throw "Could not install the pinned SAM2 source into the tracker environment."
    }
}
finally {
    Remove-Item Env:SAM2_BUILD_CUDA -ErrorAction SilentlyContinue
}

& $providerPython -c "import cv2, httpx, numpy, sam2, torch; print('SAM2 tracker ready; CUDA:', torch.cuda.is_available())"
if ($LASTEXITCODE -ne 0) {
    throw "SAM2 tracker import validation failed."
}
