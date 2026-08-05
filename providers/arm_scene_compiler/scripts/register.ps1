. (Join-Path $PSScriptRoot "common.ps1")
$provider = Get-ProviderRoot
$workspace = Get-WorkspaceRoot
$configFile = Join-Path $workspace "config\providers.json"
New-Item -ItemType Directory -Force -Path (Split-Path $configFile) | Out-Null

if (Test-Path -LiteralPath $configFile) {
    $document = Get-Content -LiteralPath $configFile -Raw | ConvertFrom-Json
}
else {
    $document = [pscustomobject]@{ providers = @() }
}
if ($null -eq $document.providers) {
    $document | Add-Member NoteProperty providers @() -Force
}
$entry = Get-Content -LiteralPath (
    Join-Path $provider "config_templates\provider_entry.json"
) -Raw | ConvertFrom-Json
$remaining = @($document.providers | Where-Object { $_.id -ne $entry.id })
$document.providers = @($remaining + $entry)
[System.IO.File]::WriteAllText(
    $configFile,
    ($document | ConvertTo-Json -Depth 30) + [Environment]::NewLine,
    [System.Text.UTF8Encoding]::new($false)
)
Write-Host "Registered $($entry.id) in $configFile"
