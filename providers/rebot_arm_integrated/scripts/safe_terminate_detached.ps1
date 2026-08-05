param(
    [string]$ProjectRoot = "",
    [string]$BasicUrl = "http://127.0.0.1:8791",
    [string]$IntegratedUrl = "http://127.0.0.1:8793",
    [string]$LaunchId = "manual"
)

$ErrorActionPreference = "Stop"
if (-not $ProjectRoot) {
    $ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
}
$ProviderRoot = Split-Path -Parent $PSScriptRoot
$LogRoot = Join-Path $ProviderRoot "runtime_logs"
$LogPath = Join-Path $LogRoot "safe_terminate.log"
$AuthoritativeStop = Join-Path $PSScriptRoot "stop_physical_gui_test.ps1"
New-Item -ItemType Directory -Force -Path $LogRoot | Out-Null

try {
    $Started = "{0} Authoritative safe termination started. launch_id={1}" -f ([DateTime]::Now.ToString("yyyy-MM-dd HH:mm:ss.fff")), $LaunchId
    Add-Content -LiteralPath $LogPath -Value $Started
    & $AuthoritativeStop -ProjectRoot $ProjectRoot -BasicUrl $BasicUrl -IntegratedUrl $IntegratedUrl -StopCore *>&1 |
        Tee-Object -FilePath $LogPath -Append
    $Completed = "{0} Authoritative safe termination completed." -f ([DateTime]::Now.ToString("yyyy-MM-dd HH:mm:ss.fff"))
    Add-Content -LiteralPath $LogPath -Value $Completed
}
catch {
    $Failed = "{0} SAFE TERMINATION FAILED: {1}" -f ([DateTime]::Now.ToString("yyyy-MM-dd HH:mm:ss.fff")), $_.Exception.Message
    Add-Content -LiteralPath $LogPath -Value $Failed
    exit 2
}
