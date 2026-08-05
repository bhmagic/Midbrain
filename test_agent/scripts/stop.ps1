param([switch]$Quiet)
. (Join-Path $PSScriptRoot "common.ps1")
$agent = Get-AgentRoot
$file = Join-Path $agent "run\pid.json"
if (Test-Path $file) {
    $data=Get-Content $file | ConvertFrom-Json
    $launcherProperty = $data.PSObject.Properties["launcher"]
    if ($null -ne $launcherProperty) {
        Stop-Process `
            -Id $launcherProperty.Value `
            -Force `
            -ErrorAction SilentlyContinue
    }
    $uiProperty = $data.PSObject.Properties["ui"]
    if ($null -ne $uiProperty) {
        Stop-Process -Id $uiProperty.Value -Force -ErrorAction SilentlyContinue
    }
    Remove-Item $file -Force -ErrorAction SilentlyContinue
}
$orphanListenerPid = Get-VerifiedAgentUiListenerProcessId
if ($null -ne $orphanListenerPid) {
    Stop-Process `
        -Id $orphanListenerPid `
        -Force `
        -ErrorAction SilentlyContinue
}
if (-not $Quiet) { Write-Host "Test-agent UI stopped." }
