param(
    [string]$ManagerUrl = "http://127.0.0.1:7001",
    [string]$FabricUrl = "http://127.0.0.1:7002",
    [string]$OpenAIModel = ""
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

& $python @arguments
exit $LASTEXITCODE
