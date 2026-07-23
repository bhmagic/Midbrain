. (Join-Path $PSScriptRoot "common.ps1")
$workspace = Get-WorkspaceRoot
$configFile = Join-Path $workspace "config\providers.json"
if (-not (Test-Path $configFile)) { return }
$document = Get-Content $configFile -Raw | ConvertFrom-Json
$document.providers = @($document.providers | Where-Object { $_.id -ne "robot_arm.rebot_dm" })
[System.IO.File]::WriteAllText($configFile, ($document | ConvertTo-Json -Depth 30) + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))
Write-Host "Unregistered robot_arm.rebot_dm"
