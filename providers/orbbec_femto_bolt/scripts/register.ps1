. (Join-Path $PSScriptRoot "common.ps1")
$provider = Get-ProviderRoot
$workspace = Get-WorkspaceRoot
$configDir = Join-Path $workspace "config"
$configFile = Join-Path $configDir "providers.json"
New-Item -ItemType Directory -Force -Path $configDir | Out-Null

if (Test-Path $configFile) {
    $document = Get-Content $configFile -Raw | ConvertFrom-Json
}
else {
    $document = [pscustomobject]@{ providers = @() }
}
if ($null -eq $document.providers) {
    $document | Add-Member -MemberType NoteProperty -Name providers -Value @() -Force
}

$entry = Get-Content (Join-Path $provider "config_templates\provider_entry.json") -Raw | ConvertFrom-Json
$remaining = @($document.providers | Where-Object { $_.id -ne $entry.id })
$document.providers = @($remaining + $entry)
$json = $document | ConvertTo-Json -Depth 20
[System.IO.File]::WriteAllText($configFile, $json + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))
Write-Host "Registered camera.femto_bolt in $configFile"
