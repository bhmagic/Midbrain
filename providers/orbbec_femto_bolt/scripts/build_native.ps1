param(
    [string]$OrbbecIncludeDir = "C:\Program Files\OrbbecSDK 2.8.6\include",
    [string]$OrbbecLibrary = "C:\Program Files\OrbbecSDK 2.8.6\lib\OrbbecSDK.lib",
    [string]$OrbbecBinDir = "C:\Program Files\OrbbecSDK 2.8.6\bin",
    [switch]$Clean
)

$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "common.ps1")

# Windows normally treats environment-variable names case-insensitively, but
# MSBuild's child-process launcher rejects a process block containing both Path
# and PATH. Normalize only that duplicate while preserving the effective value.
$processPathEntries = @(
    [Environment]::GetEnvironmentVariables("Process").GetEnumerator() |
        Where-Object { $_.Key -ieq "Path" }
)
if ($processPathEntries.Count -gt 1) {
    $processPathValue = (
        $processPathEntries |
            Where-Object { $_.Key -ceq "Path" } |
            Select-Object -First 1
    ).Value
    if (-not $processPathValue) {
        $processPathValue = $processPathEntries[0].Value
    }
    foreach ($entry in $processPathEntries) {
        [Environment]::SetEnvironmentVariable(
            [string]$entry.Key,
            $null,
            "Process"
        )
    }
    [Environment]::SetEnvironmentVariable(
        "Path",
        [string]$processPathValue,
        "Process"
    )
}

$provider = Get-ProviderRoot
$source = Join-Path $provider "native_host"
$build = Join-Path $source "build"
$release = Join-Path $build "Release"

$requiredSdkFiles = @(
    (Join-Path $OrbbecIncludeDir "libobsensor\ObSensor.hpp"),
    $OrbbecLibrary,
    (Join-Path $OrbbecBinDir "OrbbecSDK.dll")
)

foreach ($required in $requiredSdkFiles) {
    if (-not (Test-Path $required)) {
        throw "Missing Orbbec SDK file: $required"
    }
}

$cmakeCommand = Get-Command cmake -ErrorAction SilentlyContinue
if (-not $cmakeCommand) {
    $vswhere = "C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe"
    if (Test-Path -LiteralPath $vswhere) {
        $vsInstall = & $vswhere -latest -products * -property installationPath
        if ($vsInstall) {
            $bundledCmake = Join-Path $vsInstall "Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
            if (Test-Path -LiteralPath $bundledCmake) {
                $cmakeCommand = Get-Item -LiteralPath $bundledCmake
            }
        }
    }
}
if (-not $cmakeCommand) {
    throw "CMake is unavailable. Install CMake or the Visual Studio CMake component."
}
$cmakeExe = if ($cmakeCommand -is [System.IO.FileSystemInfo]) {
    $cmakeCommand.FullName
} else {
    $cmakeCommand.Source
}

$extensionCandidates = @(
    (Join-Path $OrbbecBinDir "extensions"),
    (Join-Path (Split-Path $OrbbecBinDir -Parent) "extensions"),
    "C:\Program Files\OrbbecSDK 2.8.6\bin\extensions",
    "C:\Program Files\OrbbecSDK 2.8.6\extensions"
)

$extensionSource = $extensionCandidates |
    Where-Object {
        $_ -and
        (Test-Path (Join-Path $_ "frameprocessor\ob_frame_processor.dll")) -and
        (Test-Path (Join-Path $_ "depthengine\depthengine.dll"))
    } |
    Select-Object -First 1

if (-not $extensionSource) {
    $searched = $extensionCandidates -join "`n  - "
    throw @"
Required Orbbec runtime extensions were not found.
The RGB stream can work without these files, but depth and RGB-D calibration cannot.
Searched:
  - $searched
Expected files:
  extensions\frameprocessor\ob_frame_processor.dll
  extensions\depthengine\depthengine.dll
"@
}

if ($Clean -and (Test-Path $build)) {
    Remove-Item $build -Recurse -Force
}

$arguments = @(
    "-S", $source,
    "-B", $build,
    "-G", "Visual Studio 17 2022",
    "-A", "x64",
    "-DORBBEC_INCLUDE_DIR=$OrbbecIncludeDir",
    "-DORBBEC_LIBRARY=$OrbbecLibrary",
    "-DORBBEC_BIN_DIR=$OrbbecBinDir",
    "-DORBBEC_EXTENSIONS_DIR=$extensionSource"
)

Write-Host "Configuring CameraHost"
Write-Host "Orbbec extensions: $extensionSource"
& $cmakeExe @arguments
if ($LASTEXITCODE -ne 0) {
    throw "CMake configuration failed."
}

Write-Host "Building CameraHost Release"
& $cmakeExe --build $build --config Release --parallel
if ($LASTEXITCODE -ne 0) {
    throw "CameraHost build failed."
}

$exe = Join-Path $release "CameraHost.exe"
if (-not (Test-Path $exe)) {
    throw "Expected output missing: $exe"
}

# CMake normally copies the runtime extensions. Repeat the copy explicitly as a
# Windows packaging safeguard because Orbbec's extension loader resolves paths
# relative to CameraHost.exe and CMake auto-discovery has varied by SDK installer.
$extensionTarget = Join-Path $release "extensions"
if (Test-Path $extensionTarget) {
    Remove-Item $extensionTarget -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $extensionTarget | Out-Null
Copy-Item -Recurse -Force -Path (Join-Path $extensionSource "*") -Destination $extensionTarget
Write-Host "Copied Orbbec SDK extensions: $extensionSource -> $extensionTarget"

$requiredRuntimeFiles = @(
    $exe,
    (Join-Path $release "OrbbecSDK.dll"),
    (Join-Path $extensionTarget "frameprocessor\ob_frame_processor.dll"),
    (Join-Path $extensionTarget "depthengine\depthengine.dll")
)

foreach ($required in $requiredRuntimeFiles) {
    if (-not (Test-Path $required)) {
        throw "CameraHost runtime package is incomplete: $required"
    }
}

Write-Host "Validated CameraHost runtime package:"
$requiredRuntimeFiles | ForEach-Object { Write-Host "  $_" }
Write-Host "Built $exe"
