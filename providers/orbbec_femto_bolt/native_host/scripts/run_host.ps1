param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string]$MappingName = "Local\FemtoBoltPipeline_CameraHost_v2",
    [switch]$NoColor,
    [switch]$NoDepth,
    [switch]$NoIr,
    [switch]$NoImu,
    [switch]$NoFrameSync,
    [switch]$NoHardwareD2C,
    [switch]$NoAlignedDepth,
    [switch]$NoPointCloud,
    [switch]$RgbPointCloudExperimental,
    [string[]]$ExtraArgs = @()
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$exe = Join-Path $ProjectRoot "build\Release\CameraHost.exe"
if (-not (Test-Path $exe)) {
    throw "CameraHost.exe not found. Run scripts\build_release.ps1 first."
}

$args = @("--mapping-name", $MappingName)
if ($NoColor) { $args += "--no-color" }
if ($NoDepth) { $args += "--no-depth" }
if ($NoIr) { $args += "--no-ir" }
if ($NoImu) { $args += "--no-imu" }
if ($NoFrameSync) { $args += "--no-frame-sync" }
if ($NoHardwareD2C) { $args += "--no-hardware-d2c" }
if ($NoAlignedDepth) { $args += "--no-aligned-depth" }
if ($NoPointCloud) { $args += "--no-point-cloud" }
if ($RgbPointCloudExperimental) { $args += "--rgb-point-cloud-experimental" }
if ($ExtraArgs.Count -gt 0) { $args += $ExtraArgs }

& $exe @args
