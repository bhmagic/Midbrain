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

$keyFile = Join-Path $workspaceRoot "config\api_keys.env"
if (-not (Test-Path -LiteralPath $keyFile)) {
    Copy-Item -LiteralPath (
        Join-Path $workspaceRoot "config\api_keys.env.example"
    ) -Destination $keyFile
}
$keyName = "MIDBRAIN_CONTACT_SLICING_SECRET"
$keyLines = @(Get-Content -LiteralPath $keyFile)
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
    [IO.File]::WriteAllLines(
        $keyFile,
        $keyLines,
        [Text.UTF8Encoding]::new($false)
    )
    Write-Host "Generated the local slicing authorization secret in $keyFile"
}
else {
    Write-Host "Preserved the existing local slicing authorization secret."
}
Write-Host "Contact Provider environment ready: $venv"
Write-Host "Local controller configuration: $controllerConfig"
Write-Host "The uncommitted slicing authorization secret is shared only through local process configuration."
