param(
    [string]$OutputDirectory = "dist"
)
. (Join-Path $PSScriptRoot "common.ps1")
$provider = Get-ProviderRoot
$python = Get-PythonPath
if (-not (Test-Path $python)) { throw "Basic Controller environment is missing. Run scripts\setup.ps1 first." }
$output = if ([System.IO.Path]::IsPathRooted($OutputDirectory)) { $OutputDirectory } else { Join-Path $provider $OutputDirectory }
New-Item -ItemType Directory -Force -Path $output | Out-Null
& $python -m pip wheel (Join-Path $provider "python") --no-deps --no-build-isolation --wheel-dir $output
if ($LASTEXITCODE -ne 0) { throw "Wheel build failed." }
Write-Host "Wheel output: $output"
