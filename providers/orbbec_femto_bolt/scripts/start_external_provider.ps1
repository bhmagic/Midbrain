param(
    [string]$ManagerUrl = "http://127.0.0.1:7001",
    [string]$FabricUrl = "http://127.0.0.1:7002",
    [ValidateRange(1, 65535)]
    [int]$ControlPort = 7101,
    [string]$MappingName = "Local\FemtoBoltPipeline_CameraHost_v2",
    [ValidateSet("none", "xyz", "xyzrgb")]
    [string]$PointCloudMode = "xyz",
    [string]$LogStem = "external-provider"
)

$ErrorActionPreference = "Stop"
$ProviderRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$WorkspaceRoot = (Resolve-Path (Join-Path $ProviderRoot "..\..")).Path
$Python = Join-Path $ProviderRoot ".venv\Scripts\python.exe"
$Provider = Join-Path $ProviderRoot "provider.py"
$NativeHost = Join-Path $ProviderRoot "native_host\build\Release\CameraHost.exe"
$RunRoot = Join-Path $ProviderRoot "run"

foreach ($RequiredPath in @($Python, $Provider, $NativeHost)) {
    if (-not (Test-Path -LiteralPath $RequiredPath -PathType Leaf)) {
        throw "Required camera provider file is missing: $RequiredPath"
    }
}
if (-not (Test-Path -LiteralPath $RunRoot -PathType Container)) {
    New-Item -ItemType Directory -Path $RunRoot | Out-Null
}

$StdoutPath = Join-Path $RunRoot "$LogStem.out.log"
$StderrPath = Join-Path $RunRoot "$LogStem.err.log"
$Arguments = @(
    $Provider,
    "--manager-url", $ManagerUrl,
    "--fabric-url", $FabricUrl,
    "--control-port", $ControlPort,
    "--native-exe", $NativeHost,
    "--mapping-name", $MappingName,
    "--point-cloud-mode", $PointCloudMode,
    "--workspace-root", $WorkspaceRoot
)

$Process = Start-Process `
    -FilePath $Python `
    -ArgumentList $Arguments `
    -WorkingDirectory $WorkspaceRoot `
    -RedirectStandardOutput $StdoutPath `
    -RedirectStandardError $StderrPath `
    -WindowStyle Hidden `
    -PassThru

[ordered]@{
    pid = $Process.Id
    started_at = [DateTimeOffset]::UtcNow.ToString("o")
    stdout = $StdoutPath
    stderr = $StderrPath
} | ConvertTo-Json -Compress
