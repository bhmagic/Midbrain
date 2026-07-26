param(
    [string]$ConfigDirectory = ""
)

. (Join-Path $PSScriptRoot "common.ps1")
$workspace = Get-WorkspaceRoot
$core = Get-CoreRoot
$config = if ($ConfigDirectory) {
    [System.IO.Path]::GetFullPath($ConfigDirectory)
}
else {
    Join-Path $workspace "config"
}
New-Item -ItemType Directory -Force -Path $config | Out-Null

function Resolve-ConfigTemplate {
    param(
        [Parameter(Mandatory = $true)][string]$FileName,
        [Parameter(Mandatory = $true)][string]$FallbackPath
    )

    $rootTemplate = Join-Path (Join-Path $workspace "config") $FileName
    if (Test-Path -LiteralPath $rootTemplate) {
        return $rootTemplate
    }
    if (Test-Path -LiteralPath $FallbackPath) {
        return $FallbackPath
    }
    throw "Missing configuration template: $FileName"
}

$systemTemplate = Resolve-ConfigTemplate `
    -FileName "system.env.example" `
    -FallbackPath (Join-Path $core "config_templates\system.env.example")
$systemTarget = Join-Path $config "system.env"
if (-not (Test-Path $systemTarget)) {
    Copy-Item -LiteralPath $systemTemplate -Destination $systemTarget
    Write-Host "Created $systemTarget"
}
else {
    Write-Host "Kept existing $systemTarget"
}

$apiKeysTemplate = Resolve-ConfigTemplate `
    -FileName "api_keys.env.example" `
    -FallbackPath (Join-Path $core "config_templates\api_keys.env.example")
$apiKeysTarget = Join-Path $config "api_keys.env"
if (-not (Test-Path $apiKeysTarget)) {
    Copy-Item -LiteralPath $apiKeysTemplate -Destination $apiKeysTarget
    Write-Host "Created blank $apiKeysTarget"
}
else {
    Write-Host "Kept existing $apiKeysTarget"
}

$providersTemplate = Resolve-ConfigTemplate `
    -FileName "providers.json.example" `
    -FallbackPath (Join-Path $core "config_templates\providers.json.example")
$providersTarget = Join-Path $config "providers.json"
if (-not (Test-Path $providersTarget)) {
    Copy-Item -LiteralPath $providersTemplate -Destination $providersTarget
    Write-Host "Created $providersTarget"
}
else {
    Write-Host "Kept existing $providersTarget"
}

Write-Host "The config directory is persistent and is not replaced by component packages."
