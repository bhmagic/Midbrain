$ports = [System.IO.Ports.SerialPort]::GetPortNames() | Sort-Object
if (-not $ports -or $ports.Count -eq 0) {
    Write-Host "No serial COM ports were found."
    exit 1
}
Write-Host "Available serial ports:"
$ports | ForEach-Object { Write-Host "  $_" }
Write-Host "Unity bridge known-good default: COM3 at 921600 baud."
