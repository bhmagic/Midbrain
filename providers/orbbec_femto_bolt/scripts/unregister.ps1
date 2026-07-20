. (Join-Path $PSScriptRoot "common.ps1")
$workspace = Get-WorkspaceRoot
$configFile = Join-Path $workspace "config\providers.json"
if (-not (Test-Path $configFile)) { return }
$document = Get-Content $configFile -Raw | ConvertFrom-Json
$document.providers = @($document.providers | Where-Object { $_.id -ne "camera.femto_bolt" })
$json = $document | ConvertTo-Json -Depth 20
[System.IO.File]::WriteAllText($configFile, $json + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))
Write-Host "Unregistered camera.femto_bolt."
