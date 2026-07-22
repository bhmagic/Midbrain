param(
    [string]$OutputDirectory = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$providerRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$version = (Get-Content (Join-Path $providerRoot "VERSION") -Raw).Trim()

if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = Join-Path $providerRoot "dist"
}

New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
$outputRoot = (Resolve-Path $OutputDirectory).Path
$manifestPath = Join-Path $providerRoot "FILE_MANIFEST.sha256"

function Get-RelativeProviderPath {
    param([System.IO.FileInfo]$File)
    return $File.FullName.Substring($providerRoot.Length + 1)
}

function Test-IncludedSourceFile {
    param([string]$RelativePath)

    $normalized = $RelativePath.Replace("\", "/")
    $parts = $normalized.Split("/")
    if ($parts[0] -in @(".venv", "nvlabs", "sam2", "debug", "logs", "dist")) {
        return $false
    }
    if ($normalized -eq "FILE_MANIFEST.sha256") {
        return $false
    }
    if ($parts -contains "__pycache__" -or $parts -contains ".pytest_cache") {
        return $false
    }
    if ($normalized -match '(^|/)python/[^/]+\.egg-info(/|$)') {
        return $false
    }
    if ($normalized.EndsWith(".pyc") -or $normalized.EndsWith(".pyo")) {
        return $false
    }
    return $true
}

$sourceFiles = @(
    Get-ChildItem -LiteralPath $providerRoot -File -Recurse -Force |
        Where-Object {
            Test-IncludedSourceFile (Get-RelativeProviderPath $_)
        } |
        Sort-Object FullName
)

$manifestLines = [System.Collections.Generic.List[string]]::new()
foreach ($file in $sourceFiles) {
    $relative = (Get-RelativeProviderPath $file).Replace("\", "/")
    $digest = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    $manifestLines.Add("$digest  $relative")
}

[System.IO.File]::WriteAllLines(
    $manifestPath,
    $manifestLines,
    [System.Text.UTF8Encoding]::new($false)
)

$stageRoot = Join-Path $env:TEMP ("foundation_pose_release_" + [guid]::NewGuid().ToString("N"))
$stageProvider = Join-Path $stageRoot "foundation_pose"
$zipPath = Join-Path $outputRoot "Midbrain-foundation-pose-provider-v$version-publication.zip"

try {
    New-Item -ItemType Directory -Force -Path $stageProvider | Out-Null
    foreach ($line in Get-Content -LiteralPath $manifestPath) {
        if ([string]::IsNullOrWhiteSpace($line)) {
            continue
        }
        $expected, $relative = $line -split '  ', 2
        $source = Join-Path $providerRoot $relative
        $destination = Join-Path $stageProvider $relative
        New-Item -ItemType Directory -Force -Path (Split-Path $destination -Parent) | Out-Null
        Copy-Item -LiteralPath $source -Destination $destination -Force
        $actual = (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash
        if ($actual -ne $expected) {
            throw "Staged checksum mismatch: $relative"
        }
    }
    Copy-Item -LiteralPath $manifestPath -Destination (Join-Path $stageProvider "FILE_MANIFEST.sha256") -Force

    $python = Join-Path $providerRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw "Provider Python is required for staged validation: $python"
    }
    & $python (Join-Path $stageProvider "scripts\validate_publication.py")
    if ($LASTEXITCODE -ne 0) {
        throw "Staged publication validation failed with exit code $LASTEXITCODE"
    }

    if (Test-Path -LiteralPath $zipPath) {
        Remove-Item -LiteralPath $zipPath -Force
    }
    Compress-Archive -LiteralPath $stageProvider -DestinationPath $zipPath -CompressionLevel Optimal
    $zipHash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $checksumPath = Join-Path $outputRoot "foundation_pose_v${version}_artifacts.sha256"
    [System.IO.File]::WriteAllText(
        $checksumPath,
        "$zipHash  $([System.IO.Path]::GetFileName($zipPath))`n",
        [System.Text.UTF8Encoding]::new($false)
    )
    Write-Host "[BUILT] $zipPath"
    Write-Host "[SHA256] $zipHash"
}
finally {
    if (Test-Path -LiteralPath $stageRoot) {
        Remove-Item -LiteralPath $stageRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
