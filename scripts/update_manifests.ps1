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

    if ($normalized.StartsWith('config/')) {
        $safeConfigFiles = @(
            'config/.gitkeep',
            'config/README.md',
            'config/BASELINE_INVENTORY.md',
            'config/api_keys.env.example',
            'config/providers.json.example',
            'config/system.env.example',
            'config/foundation_pose/FILE_MANIFEST.sha256',
            'config/foundation_pose/MODIFICATIONS.md',
            'config/foundation_pose/UPSTREAM.md',
            'config/foundation_pose/models.json',
            'config/foundation_pose/licenses/CERN-OHL-W-2.0.txt',
            'config/foundation_pose/models/Base_clean_centered.obj',
            'config/foundation_pose/models/Base_clean_original_frame.obj',
            'config/foundation_pose/models/Base_mesh_metadata.json',
            'config/foundation_pose/models/Gripper_clean_centered.obj',
            'config/foundation_pose/models/Gripper_clean_original_frame.obj',
            'config/foundation_pose/models/Gripper_mesh_metadata.json',
            'config/foundation_pose/references/Base_reference_atlas.json',
            'config/foundation_pose/references/Base_reference_atlas.png',
            'config/foundation_pose/references/Gripper_reference_atlas.json',
            'config/foundation_pose/references/Gripper_reference_atlas.png',
            'config/foundation_pose/source/01_BASE_Link.step',
            'config/foundation_pose/source/01_BASE_Plate.step',
            'config/foundation_pose/source/01_Rail_Bracket.step',
            'config/foundation_pose/source/Base.obj',
            'config/foundation_pose/source/Gripper.obj'
        )
        return -not ($safeConfigFiles -contains $normalized)
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

function Get-CanonicalFileHash {
    param(
        [Parameter(Mandatory = $true)][System.IO.FileInfo]$File
    )

    $textExtensions = @(
        '.bat', '.c', '.cc', '.cfg', '.cmake', '.cmd', '.cpp', '.css', '.csv',
        '.env', '.example', '.gitattributes', '.gitignore', '.h', '.hpp', '.html',
        '.ini', '.js', '.json', '.jsx', '.lock', '.md', '.obj', '.ps1', '.py',
        '.rs', '.schema', '.sh', '.sha256', '.step', '.toml', '.ts', '.tsx', '.txt', '.xml',
        '.yaml', '.yml'
    )
    $textNames = @(
        'cargo.lock', 'cargo.toml', 'cmakelists.txt', 'dockerfile', 'license',
        'notice', 'version'
    )
    $extension = $File.Extension.ToLowerInvariant()
    $name = $File.Name.ToLowerInvariant()

    if (($textExtensions -contains $extension) -or ($textNames -contains $name)) {
        $sourceBytes = [System.IO.File]::ReadAllBytes($File.FullName)
        $encoding = [System.Text.UTF8Encoding]::new($false)
        $bom = [byte[]]@()
        $offset = 0
        if ($sourceBytes.Length -ge 3 -and
            $sourceBytes[0] -eq 0xEF -and
            $sourceBytes[1] -eq 0xBB -and
            $sourceBytes[2] -eq 0xBF) {
            $bom = [byte[]]@(0xEF, 0xBB, 0xBF)
            $offset = 3
        }
        elseif ($sourceBytes.Length -ge 2 -and
            $sourceBytes[0] -eq 0xFF -and
            $sourceBytes[1] -eq 0xFE) {
            $encoding = [System.Text.UnicodeEncoding]::new($false, $false)
            $bom = [byte[]]@(0xFF, 0xFE)
            $offset = 2
        }
        elseif ($sourceBytes.Length -ge 2 -and
            $sourceBytes[0] -eq 0xFE -and
            $sourceBytes[1] -eq 0xFF) {
            $encoding = [System.Text.UnicodeEncoding]::new($true, $false)
            $bom = [byte[]]@(0xFE, 0xFF)
            $offset = 2
        }

        $text = $encoding.GetString($sourceBytes, $offset, $sourceBytes.Length - $offset)
        $canonicalText = $text.Replace("`r`n", "`n")
        $payloadBytes = $encoding.GetBytes($canonicalText)
        $bytes = [byte[]]::new($bom.Length + $payloadBytes.Length)
        if ($bom.Length -gt 0) {
            [System.Array]::Copy($bom, 0, $bytes, 0, $bom.Length)
        }
        [System.Array]::Copy($payloadBytes, 0, $bytes, $bom.Length, $payloadBytes.Length)
        $sha = [System.Security.Cryptography.SHA256]::Create()
        try {
            return ([System.BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
        }
        finally {
            $sha.Dispose()
        }
    }

    return (Get-FileHash -Algorithm SHA256 -LiteralPath $File.FullName).Hash.ToLowerInvariant()
}

function Write-Manifest {
    param(
        [Parameter(Mandatory = $true)][string]$BaseDirectory
    )

    $workspacePath = (Resolve-Path $workspace).Path -replace '[\\/]+$', ''
    $basePath = (Resolve-Path $BaseDirectory).Path -replace '[\\/]+$', ''
    if (-not $basePath.StartsWith(
        $workspacePath,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Manifest base directory is outside the workspace: $basePath"
    }
    $baseRelativeToWorkspace = (
        $basePath.Substring($workspacePath.Length) -replace '^[\\/]+', ''
    ) -replace '\\', '/'
    $manifestPath = Join-Path $basePath 'FILE_MANIFEST.sha256'
    $manifestRelativePath = 'FILE_MANIFEST.sha256'
    $providerManifest = (Split-Path $basePath -Leaf) -in @(
        'rebot_arm_dm',
        'rebot_arm_integrated'
    )
    $relativePaths = [System.Collections.Generic.List[string]]::new()
    $filesByRelativePath = [System.Collections.Generic.Dictionary[string, System.IO.FileInfo]]::new(
        [System.StringComparer]::Ordinal
    )
    $lines = [System.Collections.Generic.List[string]]::new()

    $gitArguments = @(
        "-C",
        $workspacePath,
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard"
    )
    if ($baseRelativeToWorkspace) {
        $gitArguments += @("--", $baseRelativeToWorkspace)
    }
    $repositoryRelativePaths = @(& git @gitArguments)
    if ($LASTEXITCODE -ne 0) {
        throw "Could not enumerate Git-controlled manifest inputs for $basePath"
    }

    foreach ($repositoryRelativePath in $repositoryRelativePaths) {
        $repositoryRelativePath = $repositoryRelativePath -replace '\\', '/'
        $relativePath = if ($baseRelativeToWorkspace) {
            $repositoryRelativePath.Substring($baseRelativeToWorkspace.Length) `
                -replace '^[\\/]+', ''
        }
        else {
            $repositoryRelativePath
        }
        $fullPath = Join-Path (
            $workspacePath
        ) ($repositoryRelativePath -replace '/', '\')
        if (-not (Test-Path -LiteralPath $fullPath -PathType Leaf)) {
            continue
        }
        $file = Get-Item -LiteralPath $fullPath
        if ($providerManifest -and $relativePath -eq 'SHA256SUMS.txt') {
            continue
        }
        if (Test-ManifestFileExcluded -RelativePath $relativePath -ManifestRelativePath $manifestRelativePath) {
            continue
        }
        $relativePaths.Add($relativePath)
        $filesByRelativePath.Add($relativePath, $file)
    }

    $relativePaths.Sort([System.StringComparer]::Ordinal)
    foreach ($relativePath in $relativePaths) {
        $file = $filesByRelativePath[$relativePath]
        $hash = Get-CanonicalFileHash -File $file
        $lines.Add("$hash  $relativePath")
    }

    $content = if ($lines.Count -gt 0) { ($lines -join "`n") + "`n" } else { "" }
    [System.IO.File]::WriteAllText($manifestPath, $content, [System.Text.UTF8Encoding]::new($false))
    if ($providerManifest) {
        [System.IO.File]::WriteAllText(
            (Join-Path $basePath 'SHA256SUMS.txt'),
            $content,
            [System.Text.UTF8Encoding]::new($false)
        )
    }
    Write-Host "Updated $manifestPath ($($lines.Count) files)"
}

$componentDirectories = @(
    (Join-Path $workspace 'contracts'),
    (Join-Path $workspace 'docs\reference\project_notes'),
    (Join-Path $workspace 'platform_core'),
    (Join-Path $workspace 'providers\rebot_arm_dm'),
    (Join-Path $workspace 'providers\rebot_arm_integrated'),
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
