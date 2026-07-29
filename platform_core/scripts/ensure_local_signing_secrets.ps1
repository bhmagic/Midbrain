param(
    [string]$ConfigPath
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

. (Join-Path $PSScriptRoot "common.ps1")
$workspace = Get-WorkspaceRoot
if ([string]::IsNullOrWhiteSpace($ConfigPath)) {
    $ConfigPath = Join-Path $workspace "config\api_keys.env"
}
$resolvedConfig = [IO.Path]::GetFullPath($ConfigPath)
$resolvedWorkspace = [IO.Path]::GetFullPath($workspace)
if (-not $resolvedConfig.StartsWith(
    $resolvedWorkspace + [IO.Path]::DirectorySeparatorChar,
    [StringComparison]::OrdinalIgnoreCase
)) {
    throw "Signing-secret config must remain inside the workspace."
}
if (-not (Test-Path -LiteralPath $resolvedConfig)) {
    throw "Missing local config file: $resolvedConfig"
}

function New-LocalSigningSecret {
    $bytes = New-Object byte[] 48
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($bytes)
    }
    finally {
        $generator.Dispose()
    }
    return [Convert]::ToBase64String($bytes).
        TrimEnd("=").
        Replace("+", "-").
        Replace("/", "_")
}

$requiredNames = @(
    "MIDBRAIN_REVIEW_AUTH_SECRET",
    "MIDBRAIN_AUTHORIZATION_SECRET"
)
$lines = [Collections.Generic.List[string]]::new()
foreach ($line in [IO.File]::ReadAllLines($resolvedConfig)) {
    $lines.Add($line)
}

$generated = [Collections.Generic.List[string]]::new()
foreach ($name in $requiredNames) {
    $indices = @(
        for ($index = 0; $index -lt $lines.Count; $index++) {
            if ($lines[$index] -match ("^" + [regex]::Escape($name) + "=")) {
                $index
            }
        }
    )
    if ($indices.Count -gt 1) {
        throw "Duplicate $name entries exist in $resolvedConfig"
    }
    if ($indices.Count -eq 0) {
        $lines.Add("$name=$(New-LocalSigningSecret)")
        $generated.Add($name)
        continue
    }
    $parts = $lines[$indices[0]].Split("=", 2)
    if ($parts.Count -ne 2 -or [string]::IsNullOrWhiteSpace($parts[1])) {
        $lines[$indices[0]] = "$name=$(New-LocalSigningSecret)"
        $generated.Add($name)
    }
}

if ($generated.Count -gt 0) {
    $temporaryPath = "$resolvedConfig.$([guid]::NewGuid().ToString('N')).tmp"
    try {
        [IO.File]::WriteAllLines(
            $temporaryPath,
            $lines,
            [Text.UTF8Encoding]::new($false)
        )
        Move-Item -LiteralPath $temporaryPath -Destination $resolvedConfig -Force
    }
    finally {
        if (Test-Path -LiteralPath $temporaryPath) {
            Remove-Item -LiteralPath $temporaryPath -Force
        }
    }
}

[ordered]@{
    config_path = $resolvedConfig
    generated = @($generated)
    values_printed = $false
} | ConvertTo-Json -Compress
