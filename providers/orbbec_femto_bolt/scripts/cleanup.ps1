param([switch]$Quiet)
Get-Process CameraHost -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*providers\orbbec_femto_bolt\provider.py*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
if (-not $Quiet) { Write-Host "Orbbec provider processes cleaned up." }
