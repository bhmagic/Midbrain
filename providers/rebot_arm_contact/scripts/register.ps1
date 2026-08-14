. (Join-Path $PSScriptRoot "common.ps1")
$providerRoot = Get-ProviderRoot
$workspaceRoot = Get-WorkspaceRoot
$configFile = Join-Path $workspaceRoot "config\providers.json"
New-Item -ItemType Directory -Force -Path (Split-Path $configFile) | Out-Null
if (Test-Path $configFile) {
    $document = Get-Content $configFile -Raw | ConvertFrom-Json
}
else {
    $document = [pscustomobject]@{providers = @()}
}
if ($null -eq $document.providers) {
    $document | Add-Member NoteProperty providers @() -Force
}
$entry = Get-Content (Join-Path $providerRoot "config_templates\provider_entry.json") -Raw | ConvertFrom-Json
$document.providers = @($document.providers | Where-Object {$_.id -ne $entry.id}) + @($entry)
[IO.File]::WriteAllText(
    $configFile,
    ($document | ConvertTo-Json -Depth 30) + [Environment]::NewLine,
    [Text.UTF8Encoding]::new($false)
)
Write-Host "Registered $($entry.id) in $configFile"
