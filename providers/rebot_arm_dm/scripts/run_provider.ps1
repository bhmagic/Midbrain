param(
    [switch]$Simulate,
    [switch]$AllowHardwareCalibration,
    [switch]$ReadOnly,
    [string]$Port = "COM3",
    [int]$ListenPort = 8791,
    [string]$ManagerUrl = "http://127.0.0.1:7001",
    [string]$FabricUrl = "http://127.0.0.1:7002"
)
. (Join-Path $PSScriptRoot "common.ps1")
$provider = Get-ProviderRoot
$python = Get-PythonPath
if (-not (Test-Path $python)) { throw "Python environment is missing. Run scripts\setup.ps1 first." }
$args = @(
    (Join-Path $provider "provider.py"),
    "--config", (Join-Path $provider "config\arm_model.json"),
    "--calibration", (Join-Path $provider "config\arm_calibration.json"),
    "--port", $Port,
    "--listen-port", $ListenPort
)
if ($Simulate) { $args += "--simulate" }
if ($AllowHardwareCalibration) { $args += "--allow-hardware-calibration" }
if ($ReadOnly) { $args += "--read-only" }
if ($ManagerUrl) { $args += @("--manager-url", $ManagerUrl) }
if ($FabricUrl) { $args += @("--fabric-url", $FabricUrl) }
& $python @args
exit $LASTEXITCODE
