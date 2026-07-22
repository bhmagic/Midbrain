param(
    [string]$Revision = "2b90b9f5ceec907a1c18123530e92e794ad901a4"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$providerRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $providerRoot ".venv\Scripts\python.exe"
$samRoot = Join-Path $providerRoot "sam2"
$checkpoint = Join-Path $samRoot "checkpoints\sam2.1_hiera_base_plus.pt"
$checkpointUrl = "https://dl.fbaipublicfiles.com/segment_anything_2/092824/sam2.1_hiera_base_plus.pt"
$checkpointSha256 = "a2345aede8715ab1d5d31b4a509fb160c5a4af1970f199d9054ccfb746c004c5"

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Provider Python environment is missing. Run scripts/setup.ps1 first."
}

if (-not (Test-Path -LiteralPath (Join-Path $samRoot ".git") -PathType Container)) {
    git clone https://github.com/facebookresearch/sam2.git $samRoot
    if ($LASTEXITCODE -ne 0) {
        throw "Could not clone the official SAM2 repository."
    }
}

git -C $samRoot fetch origin $Revision --depth 1
if ($LASTEXITCODE -ne 0) {
    throw "Could not fetch pinned SAM2 revision $Revision."
}
git -C $samRoot checkout --detach $Revision
if ($LASTEXITCODE -ne 0) {
    throw "Could not check out pinned SAM2 revision $Revision."
}

$env:SAM2_BUILD_CUDA = "0"
try {
    & $python -m pip install hydra-core iopath
    if ($LASTEXITCODE -ne 0) {
        throw "Could not install SAM2 Python prerequisites."
    }
    & $python -m pip install --no-deps --no-build-isolation -e $samRoot
    if ($LASTEXITCODE -ne 0) {
        throw "Could not install SAM2 into the Provider environment."
    }
}
finally {
    Remove-Item Env:SAM2_BUILD_CUDA -ErrorAction SilentlyContinue
}

New-Item -ItemType Directory -Force -Path (Split-Path $checkpoint -Parent) | Out-Null
if (-not (Test-Path -LiteralPath $checkpoint -PathType Leaf)) {
    curl.exe --fail --location --output $checkpoint $checkpointUrl
    if ($LASTEXITCODE -ne 0) {
        throw "Could not download the official SAM2.1 Base+ checkpoint."
    }
}

$actualHash = (Get-FileHash -LiteralPath $checkpoint -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualHash -ne $checkpointSha256) {
    throw "SAM2 checkpoint checksum mismatch: $actualHash"
}

Write-Host "[READY] SAM2 revision: $Revision"
Write-Host "[READY] SAM2.1 Base+ checkpoint SHA-256: $actualHash"
