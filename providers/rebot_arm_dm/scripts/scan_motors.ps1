param(
    [string]$Port = "COM3",
    [int]$Baudrate = 921600
)
. (Join-Path $PSScriptRoot "common.ps1")
$provider = Get-ProviderRoot
$cli = Join-Path $provider ".venv\Scripts\motorbridge-cli.exe"
if (-not (Test-Path $cli)) {
    throw "MotorBridge CLI is missing. Run scripts\setup.ps1 -WithMotorBridge first."
}
& $cli scan --vendor damiao --transport dm-serial --serial-port $Port --serial-baud $Baudrate
exit $LASTEXITCODE
