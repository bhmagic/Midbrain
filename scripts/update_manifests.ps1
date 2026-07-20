[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$workspace = Split-Path $PSScriptRoot -Parent

function Test-ManifestFileExcluded {
    param(
        [Parameter(Mandatory = $true)][string]$RelativePath,
        [Parameter(Mandatory = $true)][string]$ManifestRelativePath
    )

    $normalized = $RelativePath -replace '\\', '/'
    if ($normalized -eq $ManifestRelativePath) {
        return $true
    }

    $segments = $normalized -split '/'
    $excludedDirectories = @(
        '.git', '.validation', '.venv', 'venv', 'target', 'build', 'dist',
        '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache'
    )
    foreach ($segment in $segments) {
        if ($excludedDirectories -contains $segment -or $segment -like '*.egg-info' -or $segment -like '*.dist-info') {
            return $true
        }
    }

    if ($normalized -match '(^|/)calibration/devices/') {
        return $true
    }
    if ($normalized -match '(^|/)(logs?|run)/' -and -not $normalized.EndsWith('/.gitkeep')) {
        return $true
    }
    if ($normalized -match '\.(pyc|pyo|exe|dll|lib|pdb|rar|7z)$') {
        return $true
    }
    if ($normalized.EndsWith('/.DS_Store') -or $normalized -eq '.DS_Store') {
        return $true
    }

    return $false
}

function Write-Manifest {
    param(
        [Parameter(Mandatory = $true)][string]$BaseDirectory
    )

    $basePath = (Resolve-Path $BaseDirectory).Path -replace '[\\/]+$', ''
    $manifestPath = Join-Path $basePath 'FILE_MANIFEST.sha256'
    $manifestRelativePath = 'FILE_MANIFEST.sha256'
    $lines = [System.Collections.Generic.List[string]]::new()

    $files = Get-ChildItem -Path $basePath -Recurse -File | Sort-Object FullName
    foreach ($file in $files) {
        $relativePath = $file.FullName.Substring($basePath.Length) -replace '^[\\/]+', ''
        $relativePath = $relativePath -replace '\\', '/'
        if (Test-ManifestFileExcluded -RelativePath $relativePath -ManifestRelativePath $manifestRelativePath) {
            continue
        }
        $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $file.FullName).Hash.ToLowerInvariant()
        $lines.Add("$hash  $relativePath")
    }

    $content = if ($lines.Count -gt 0) { ($lines -join "`n") + "`n" } else { "" }
    [System.IO.File]::WriteAllText($manifestPath, $content, [System.Text.UTF8Encoding]::new($false))
    Write-Host "Updated $manifestPath ($($lines.Count) files)"
}

$componentDirectories = @(
    (Join-Path $workspace 'contracts'),
    (Join-Path $workspace 'docs\reference\project_notes'),
    (Join-Path $workspace 'platform_core'),
    (Join-Path $workspace 'providers\local_vio'),
    (Join-Path $workspace 'providers\orbbec_femto_bolt'),
    (Join-Path $workspace 'test_agent')
)

foreach ($directory in $componentDirectories) {
    if (Test-Path $directory) {
        Write-Manifest -BaseDirectory $directory
    }
}

# Generate the repository-wide manifest last so it records the component manifests.
Write-Manifest -BaseDirectory $workspace
