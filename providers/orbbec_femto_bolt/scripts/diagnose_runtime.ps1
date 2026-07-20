param(
    [string]$OrbbecBinDir = "C:\Program Files\OrbbecSDK 2.8.6\bin"
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")

$provider = Get-ProviderRoot
$release = Join-Path $provider "native_host\build\Release"

$checks = @(
    [pscustomobject]@{ Name = "CameraHost.exe"; Path = (Join-Path $release "CameraHost.exe") },
    [pscustomobject]@{ Name = "OrbbecSDK.dll"; Path = (Join-Path $release "OrbbecSDK.dll") },
    [pscustomobject]@{ Name = "Frame processor extension"; Path = (Join-Path $release "extensions\frameprocessor\ob_frame_processor.dll") },
    [pscustomobject]@{ Name = "Depth engine extension"; Path = (Join-Path $release "extensions\depthengine\depthengine.dll") },
    [pscustomobject]@{ Name = "SDK frame processor source"; Path = (Join-Path $OrbbecBinDir "extensions\frameprocessor\ob_frame_processor.dll") },
    [pscustomobject]@{ Name = "SDK depth engine source"; Path = (Join-Path $OrbbecBinDir "extensions\depthengine\depthengine.dll") }
)

$result = $checks | ForEach-Object {
    [pscustomobject]@{
        Name = $_.Name
        Exists = Test-Path $_.Path
        Path = $_.Path
    }
}

$result | Format-Table -AutoSize

$missingRuntime = $result |
    Where-Object {
        $_.Name -in @(
            "CameraHost.exe",
            "OrbbecSDK.dll",
            "Frame processor extension",
            "Depth engine extension"
        ) -and -not $_.Exists
    }

if ($missingRuntime) {
    throw "CameraHost runtime package is incomplete. Run scripts\setup.ps1 again."
}

Write-Host "CameraHost runtime package is complete."
