param([string]$PythonLauncher = "py")
. (Join-Path $PSScriptRoot "common.ps1")
$providerRoot = Get-ProviderRoot
$workspaceRoot = Get-WorkspaceRoot
$venv = Join-Path $providerRoot ".venv"
$python = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $python)) {
    if ($PythonLauncher -eq "py") {
        & py -3.11 -m venv $venv
    }
    else {
        & $PythonLauncher -m venv $venv
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Could not create the Contact Provider Python 3.11 environment."
    }
}
& $python -m pip install --upgrade pip setuptools wheel
if ($LASTEXITCODE -ne 0) { throw "Could not update packaging tools." }
& $python -m pip install -e (Join-Path $providerRoot "python")
if ($LASTEXITCODE -ne 0) { throw "Contact Provider package installation failed." }
$configDirectory = Join-Path $providerRoot "config"
$controllerConfig = Join-Path $configDirectory "controller.json"
New-Item -ItemType Directory -Force -Path $configDirectory | Out-Null
if (-not (Test-Path $controllerConfig)) {
    Copy-Item (Join-Path $providerRoot "config_templates\controller.default.json") $controllerConfig
}
$controllerDocument = Get-Content -LiteralPath $controllerConfig -Raw | ConvertFrom-Json
if ($null -eq $controllerDocument.authorization) {
    $controllerDocument | Add-Member NoteProperty authorization ([pscustomobject]@{}) -Force
}
if ($null -eq $controllerDocument.authorization.skill_secret_envs) {
    $controllerDocument.authorization | Add-Member NoteProperty skill_secret_envs ([pscustomobject]@{}) -Force
}
$requiredSkillSecrets = [ordered]@{
    "contact.slicing" = "MIDBRAIN_CONTACT_SLICING_SECRET"
    "grip.grip" = "MIDBRAIN_CONTACT_GRIP_GENERIC_SECRET"
    "grip.grip_object" = "MIDBRAIN_CONTACT_GRIP_OBJECT_SECRET"
    "contact.move_carried_object" = "MIDBRAIN_CONTACT_MOVE_CARRIED_OBJECT_SECRET"
    "grip.lay_flat" = "MIDBRAIN_CONTACT_LAY_FLAT_SECRET"
}
foreach ($entry in $requiredSkillSecrets.GetEnumerator()) {
    $controllerDocument.authorization.skill_secret_envs |
        Add-Member NoteProperty $entry.Key $entry.Value -Force
}
[IO.File]::WriteAllText(
    $controllerConfig,
    ($controllerDocument | ConvertTo-Json -Depth 30) + [Environment]::NewLine,
    [Text.UTF8Encoding]::new($false)
)

$keyFile = Join-Path $workspaceRoot "config\api_keys.env"
if (-not (Test-Path -LiteralPath $keyFile)) {
    Copy-Item -LiteralPath (
        Join-Path $workspaceRoot "config\api_keys.env.example"
    ) -Destination $keyFile
}
$keyLines = @(Get-Content -LiteralPath $keyFile)
$keyNames = @(
    "MIDBRAIN_CONTACT_SLICING_SECRET",
    "MIDBRAIN_CONTACT_GRIP_GENERIC_SECRET",
    "MIDBRAIN_CONTACT_GRIP_OBJECT_SECRET",
    "MIDBRAIN_CONTACT_MOVE_CARRIED_OBJECT_SECRET",
    "MIDBRAIN_CONTACT_LAY_FLAT_SECRET"
)
foreach ($keyName in $keyNames) {
    $keyIndex = -1
    for ($index = 0; $index -lt $keyLines.Count; $index++) {
        if ($keyLines[$index].StartsWith("$keyName=")) {
            $keyIndex = $index
            break
        }
    }
    $configuredSecret = if ($keyIndex -ge 0) {
        $keyLines[$keyIndex].Substring($keyName.Length + 1)
    }
    else {
        ""
    }
    if ([Text.Encoding]::UTF8.GetByteCount($configuredSecret) -lt 32) {
        $secretBytes = New-Object byte[] 48
        $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
        try {
            $generator.GetBytes($secretBytes)
        }
        finally {
            $generator.Dispose()
        }
        $generatedSecret = [Convert]::ToBase64String($secretBytes)
        if ($keyIndex -ge 0) {
            $keyLines[$keyIndex] = "$keyName=$generatedSecret"
        }
        else {
            $keyLines += "$keyName=$generatedSecret"
        }
        Write-Host "Generated local authorization secret $keyName in $keyFile"
    }
    else {
        Write-Host "Preserved local authorization secret $keyName."
    }
}
[IO.File]::WriteAllLines(
    $keyFile,
    $keyLines,
    [Text.UTF8Encoding]::new($false)
)
Write-Host "Contact Provider environment ready: $venv"
Write-Host "Local controller configuration: $controllerConfig"
Write-Host "The uncommitted Contact authorization secrets are shared only through local process configuration."
