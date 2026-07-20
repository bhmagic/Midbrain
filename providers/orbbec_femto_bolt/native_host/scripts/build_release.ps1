param(
    [string]$ProjectRoot = "C:\Projects\FemtoBoltPipeline\OrbbecCameraHost",
    [string]$OrbbecSdkDir = "",
    [string]$OrbbecSdkRoot = "",
    [string]$OrbbecIncludeDir = "",
    [string]$OrbbecLibrary = "",
    [string]$OrbbecBinDir = "",
    [switch]$Clean
)

$ErrorActionPreference = "Stop"

function Invoke-Checked {
    param(
        [string]$Exe,
        [string[]]$Arguments
    )

    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Exe failed with exit code $LASTEXITCODE"
    }
}

if (-not (Test-Path $ProjectRoot)) {
    throw "ProjectRoot does not exist: $ProjectRoot"
}

$ProjectRoot = (Resolve-Path $ProjectRoot).Path
$buildDir = Join-Path $ProjectRoot "build"

if ($OrbbecSdkDir -eq "" -and $OrbbecSdkRoot -eq "" -and $OrbbecIncludeDir -eq "" -and $OrbbecLibrary -eq "") {
    $defaultRoots = @(
        "C:\Program Files\OrbbecSDK 2.8.6",
        "C:\Program Files\OrbbecSDK",
        "C:\Program Files\Orbbec\OrbbecSDK"
    )

    $programFilesRoots = Get-ChildItem -Path "C:\Program Files" -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match "^OrbbecSDK" } |
        ForEach-Object { $_.FullName }

    foreach ($candidate in ($defaultRoots + $programFilesRoots)) {
        if ((Test-Path (Join-Path $candidate "include\libobsensor\ObSensor.hpp")) -and
            (Test-Path (Join-Path $candidate "lib\OrbbecSDK.lib"))) {
            $OrbbecSdkRoot = $candidate
            Write-Host "Auto-detected Orbbec SDK root: $OrbbecSdkRoot"
            break
        }
    }
}


# Normalize Orbbec SDK root into explicit include/lib/bin paths.
# This is more reliable than passing only ORBBEC_SDK_ROOT through CMake,
# especially when the SDK path contains spaces.
if ($OrbbecSdkRoot -ne "") {
    if ($OrbbecIncludeDir -eq "") {
        $candidateInclude = Join-Path $OrbbecSdkRoot "include"
        if (Test-Path (Join-Path $candidateInclude "libobsensor\ObSensor.hpp")) {
            $OrbbecIncludeDir = $candidateInclude
        }
    }
    if ($OrbbecLibrary -eq "") {
        $candidateLibrary = Join-Path $OrbbecSdkRoot "lib\OrbbecSDK.lib"
        if (Test-Path $candidateLibrary) {
            $OrbbecLibrary = $candidateLibrary
        }
    }
    if ($OrbbecBinDir -eq "") {
        $candidateBin = Join-Path $OrbbecSdkRoot "bin"
        if (Test-Path (Join-Path $candidateBin "OrbbecSDK.dll")) {
            $OrbbecBinDir = $candidateBin
        }
    }
}

if ($Clean -and (Test-Path $buildDir)) {
    Remove-Item -Recurse -Force $buildDir
}

New-Item -ItemType Directory -Force -Path $buildDir | Out-Null

$cmakeArgs = @(
    "-S", $ProjectRoot,
    "-B", $buildDir,
    "-G", "Visual Studio 17 2022",
    "-A", "x64"
)

if ($OrbbecSdkDir -ne "") {
    $cmakeArgs += "-DOrbbecSDK_DIR=$OrbbecSdkDir"
}
if ($OrbbecSdkRoot -ne "") {
    $cmakeArgs += "-DORBBEC_SDK_ROOT=$OrbbecSdkRoot"
}
if ($OrbbecIncludeDir -ne "") {
    $cmakeArgs += "-DORBBEC_INCLUDE_DIR=$OrbbecIncludeDir"
}
if ($OrbbecLibrary -ne "") {
    $cmakeArgs += "-DORBBEC_LIBRARY=$OrbbecLibrary"
}
if ($OrbbecBinDir -ne "") {
    $cmakeArgs += "-DORBBEC_BIN_DIR=$OrbbecBinDir"
}

Write-Host "Configuring CameraHost..."
Write-Host "CMake arguments: $($cmakeArgs -join ' ')"
Invoke-Checked -Exe "cmake" -Arguments $cmakeArgs

Write-Host "Building CameraHost Release..."
Invoke-Checked -Exe "cmake" -Arguments @("--build", $buildDir, "--config", "Release", "--parallel")


# Extra runtime safety: CMake also copies these, but doing it here gives a clear
# PowerShell fallback for Orbbec installers that do not expose CMake metadata.
$targetDir = Join-Path $buildDir "Release"
$extensionCandidates = @()
if ($OrbbecBinDir -ne "") { $extensionCandidates += (Join-Path $OrbbecBinDir "extensions") }
if ($OrbbecSdkRoot -ne "") {
    $extensionCandidates += (Join-Path $OrbbecSdkRoot "bin\extensions")
    $extensionCandidates += (Join-Path $OrbbecSdkRoot "extensions")
}
if ($OrbbecIncludeDir -ne "") {
    $sdkRootFromInclude = Split-Path $OrbbecIncludeDir -Parent
    $extensionCandidates += (Join-Path $sdkRootFromInclude "bin\extensions")
    $extensionCandidates += (Join-Path $sdkRootFromInclude "extensions")
}
$defaultSdkRoot = "C:\Program Files\OrbbecSDK 2.8.6"
$extensionCandidates += (Join-Path $defaultSdkRoot "bin\extensions")
$extensionCandidates += (Join-Path $defaultSdkRoot "extensions")

$extensionSource = $extensionCandidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if ($extensionSource) {
    $extensionTarget = Join-Path $targetDir "extensions"
    New-Item -ItemType Directory -Force -Path $extensionTarget | Out-Null
    Copy-Item -Recurse -Force -Path (Join-Path $extensionSource "*") -Destination $extensionTarget
    Write-Host "Copied Orbbec SDK extensions: $extensionSource -> $extensionTarget"
} else {
    Write-Warning "Could not find Orbbec SDK extensions folder. Depth/IR may fail until SDK bin\extensions is copied to build\Release\extensions."
}

$exePath = Join-Path $buildDir "Release\CameraHost.exe"
if (-not (Test-Path $exePath)) {
    throw "Build finished but CameraHost.exe was not found: $exePath"
}

Write-Host "Built: $exePath"
