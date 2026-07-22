param(
    [ValidateSet("nvlabs", "mock")]
    [string]$Backend = "nvlabs"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

. (Join-Path $PSScriptRoot "common.ps1")

$provider = Get-ProviderRoot
$workspace = Get-WorkspaceRoot
$python = Join-Path $provider ".venv\Scripts\python.exe"
$registry = Join-Path $workspace "config\foundation_pose\models.json"

$arguments = @(
    (Join-Path $provider "provider.py"),
    "--manager-url", "http://127.0.0.1:7001",
    "--fabric-url", "http://127.0.0.1:7002",
    "--control-port", "7103",
    "--backend", $Backend,
    "--model-registry", $registry
)

if ($Backend -eq "nvlabs") {
    $arguments += @(
        "--foundationpose-root",
        (Join-Path $provider "nvlabs\FoundationPose")
    )
}

& $python @arguments
