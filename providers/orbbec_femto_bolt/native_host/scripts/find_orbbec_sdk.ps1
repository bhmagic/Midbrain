$ErrorActionPreference = "Stop"

$roots = @()

$envNames = @(
    "ORBBEC_SDK_ROOT",
    "ORBBECSDK_ROOT",
    "ORBBEC_HOME",
    "OrbbecSDK_ROOT",
    "OrbbecSDK_DIR"
)

foreach ($name in $envNames) {
    $value = [Environment]::GetEnvironmentVariable($name, "Process")
    if (-not $value) { $value = [Environment]::GetEnvironmentVariable($name, "User") }
    if (-not $value) { $value = [Environment]::GetEnvironmentVariable($name, "Machine") }
    if ($value) { $roots += $value }
}

$roots += @(
    "C:\Program Files\Orbbec",
    "C:\Program Files\OrbbecSDK 2.8.6",
    "C:\Program Files\OrbbecSDK",
    "C:\Program Files\Orbbec\OrbbecSDK",
    "C:\OrbbecSDK",
    "C:\OrbbecSDK_v2"
)

$roots += Get-ChildItem -Path "C:\Program Files" -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match "^Orbbec" } |
    ForEach-Object { $_.FullName }

$orbbecCompanyDir = "C:\Program Files\Orbbec"
if (Test-Path $orbbecCompanyDir) {
    $roots += Get-ChildItem -Path $orbbecCompanyDir -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match "OrbbecSDK" } |
        ForEach-Object { $_.FullName }
}

$roots += Get-ChildItem -Path "C:\" -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match "^Orbbec" } |
    ForEach-Object { $_.FullName }

$roots = $roots | Where-Object { $_ -and (Test-Path $_) } | Select-Object -Unique

Write-Host "Candidate Orbbec roots:"
foreach ($root in $roots) { Write-Host "  $root" }
Write-Host ""

$configs = foreach ($root in $roots) {
    Get-ChildItem -Path $root -Recurse -File -Include "OrbbecSDKConfig.cmake", "orbbecsdk-config.cmake" -ErrorAction SilentlyContinue
}

$headers = foreach ($root in $roots) {
    Get-ChildItem -Path $root -Recurse -File -Filter "ObSensor.hpp" -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -match "libobsensor" }
}

$libs = foreach ($root in $roots) {
    Get-ChildItem -Path $root -Recurse -File -Include "OrbbecSDK.lib", "obsensor.lib", "libobsensor.lib" -ErrorAction SilentlyContinue
}

$dlls = foreach ($root in $roots) {
    Get-ChildItem -Path $root -Recurse -File -Include "OrbbecSDK.dll", "obsensor.dll", "libobsensor.dll" -ErrorAction SilentlyContinue
}

$extensions = foreach ($root in $roots) {
    Get-ChildItem -Path $root -Recurse -File -Include "ob_frame_processor.dll", "depthengine.dll" -ErrorAction SilentlyContinue
}

Write-Host "CMake package configs:"
if ($configs) { $configs | ForEach-Object { Write-Host "  $($_.FullName)" } } else { Write-Host "  Not found" }
Write-Host ""

Write-Host "Headers:"
if ($headers) { $headers | ForEach-Object { Write-Host "  $($_.FullName)" } } else { Write-Host "  Not found" }
Write-Host ""

Write-Host "Import libraries:"
if ($libs) { $libs | ForEach-Object { Write-Host "  $($_.FullName)" } } else { Write-Host "  Not found" }
Write-Host ""

Write-Host "Runtime DLLs:"
if ($dlls) { $dlls | ForEach-Object { Write-Host "  $($_.FullName)" } } else { Write-Host "  Not found" }
Write-Host ""

Write-Host "Runtime extension DLLs:"
if ($extensions) { $extensions | ForEach-Object { Write-Host "  $($_.FullName)" } } else { Write-Host "  Not found" }
Write-Host ""

if ($configs) {
    $dir = Split-Path $configs[0].FullName -Parent
    Write-Host "Suggested build command using package config:"
    Write-Host ".\scripts\build_release.ps1 -ProjectRoot `"$PWD`" -OrbbecSdkDir `"$dir`" -Clean"
} elseif ($headers -and $libs) {
    $includeDir = $headers[0].Directory.Parent.FullName
    $lib = $libs[0].FullName
    $binDir = if ($dlls) { $dlls[0].Directory.FullName } else { "" }
    Write-Host "Suggested build command using manual include/lib:"
    if ($binDir) {
        Write-Host ".\scripts\build_release.ps1 -ProjectRoot `"$PWD`" -OrbbecIncludeDir `"$includeDir`" -OrbbecLibrary `"$lib`" -OrbbecBinDir `"$binDir`" -Clean"
    } else {
        Write-Host ".\scripts\build_release.ps1 -ProjectRoot `"$PWD`" -OrbbecIncludeDir `"$includeDir`" -OrbbecLibrary `"$lib`" -Clean"
    }
} else {
    Write-Host "No complete native Orbbec SDK install was found. Install/extract Orbbec SDK for Windows, then rerun this script."
}
