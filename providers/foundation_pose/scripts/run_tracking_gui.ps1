param(
    [string]$ManagerUrl = "http://127.0.0.1:7001",
    [string]$FabricUrl = "http://127.0.0.1:7002",
    [string]$OpenAIModel = "",
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$providerRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $providerRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    throw "FoundationPose Provider environment is missing: $python"
}

$arguments = @(
    "-m",
    "foundation_pose_provider.gui_app",
    "--manager-url",
    $ManagerUrl,
    "--fabric-url",
    $FabricUrl
)

if ($OpenAIModel) {
    $arguments += @("--openai-model", $OpenAIModel)
}

$stopScript = Join-Path $PSScriptRoot "stop_tracking_gui.ps1"
& $stopScript -Quiet
$logsRoot = Join-Path $providerRoot "logs"
$runRoot = Join-Path $providerRoot "run"
$pidFile = Join-Path $runRoot "tracking_gui.pid.json"
New-Item -ItemType Directory -Force -Path $logsRoot, $runRoot | Out-Null
$process = Start-Process `
    -FilePath $python `
    -ArgumentList $arguments `
    -WorkingDirectory $providerRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $logsRoot "tracking_gui.out.log") `
    -RedirectStandardError (Join-Path $logsRoot "tracking_gui.err.log") `
    -PassThru
@{
    gui = $process.Id
    kind = "legacy_tk"
} | ConvertTo-Json | Set-Content -LiteralPath $pidFile -Encoding UTF8
