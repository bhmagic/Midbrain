. (Join-Path $PSScriptRoot "common.ps1")
$provider = Get-ProviderRoot
$workspace = Get-WorkspaceRoot
if (-not (Test-Path (Join-Path $workspace "platform_core"))) { throw "This provider is not currently inside a Physical AI source workspace." }
$configFile = Join-Path $workspace "config\providers.json"
New-Item -ItemType Directory -Force -Path (Split-Path $configFile) | Out-Null
if (Test-Path $configFile) { $document = Get-Content $configFile -Raw | ConvertFrom-Json } else { $document = [pscustomobject]@{ providers = @() } }
if ($null -eq $document.providers) { $document | Add-Member -MemberType NoteProperty -Name providers -Value @() -Force }
$entry = Get-Content (Join-Path $provider "config_templates\provider_entry.json") -Raw | ConvertFrom-Json
$document.providers = @($document.providers | Where-Object { $_.id -ne $entry.id }) + @($entry)
[System.IO.File]::WriteAllText($configFile, ($document | ConvertTo-Json -Depth 30) + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))
Write-Host "Registered robot_arm.rebot_dm in $configFile"
