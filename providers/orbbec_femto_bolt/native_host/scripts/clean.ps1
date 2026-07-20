param(
    [string]$ProjectRoot = "C:\Projects\FemtoBoltPipeline\OrbbecCameraHost"
)

$buildDir = Join-Path $ProjectRoot "build"
if (Test-Path $buildDir) {
    Remove-Item -Recurse -Force $buildDir
    Write-Host "Removed $buildDir"
}
