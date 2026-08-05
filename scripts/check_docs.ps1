[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$workspace = Split-Path $PSScriptRoot -Parent
$markdownPaths = @(
    & git -C $workspace ls-files --cached --others --exclude-standard -- "*.md"
)
if ($LASTEXITCODE -ne 0) {
    throw "Failed to enumerate Markdown files."
}

$markdownFiles = @(
    $markdownPaths |
        Sort-Object -Unique |
        ForEach-Object {
            $candidate = Join-Path $workspace $_
            if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                Get-Item -LiteralPath $candidate
            }
        }
)

$failures = New-Object System.Collections.Generic.List[string]
$linkPattern = '!?(?:\[[^\]]*\])\(([^)]+)\)'
$headingPattern = '^#{1,6}\s+(.+?)\s*$'

foreach ($file in $markdownFiles) {
    $relativePath = $file.FullName.Substring($workspace.Length).TrimStart('\', '/')
    $lines = @(Get-Content -LiteralPath $file.FullName -Encoding utf8)
    $headings = @{}

    for ($lineIndex = 0; $lineIndex -lt $lines.Count; $lineIndex++) {
        $lineNumber = $lineIndex + 1
        $line = $lines[$lineIndex]

        if ($line -match $headingPattern) {
            $normalizedHeading = [regex]::Replace($Matches[1].Trim().ToLowerInvariant(), '\s+', ' ')
            if ($headings.ContainsKey($normalizedHeading)) {
                $failures.Add("${relativePath}:${lineNumber}: duplicate heading '$($Matches[1].Trim())'")
            }
            else {
                $headings[$normalizedHeading] = $lineNumber
            }
        }

        foreach ($match in [regex]::Matches($line, $linkPattern)) {
            $target = $match.Groups[1].Value.Trim()
            if ($target.StartsWith('<') -and $target.EndsWith('>')) {
                $target = $target.Substring(1, $target.Length - 2)
            }
            else {
                $target = ($target -split '\s+"', 2)[0]
            }

            if ($target -match '^(?i:https?://|mailto:|app://|data:|#)') {
                continue
            }

            $target = ($target -split '#', 2)[0]
            if (-not $target) {
                continue
            }

            try {
                $decodedTarget = [uri]::UnescapeDataString($target)
                $resolvedTarget = [System.IO.Path]::GetFullPath(
                    (Join-Path $file.DirectoryName ($decodedTarget -replace '/', '\'))
                )
            }
            catch {
                $failures.Add("${relativePath}:${lineNumber}: invalid local link '$target'")
                continue
            }

            if (-not (Test-Path -LiteralPath $resolvedTarget)) {
                $failures.Add("${relativePath}:${lineNumber}: missing local link target '$target'")
            }
        }
    }
}

# Every retained Provider document must be directly discoverable from that
# Provider's landing page. This is intentionally a link-coverage rule, not a
# template rule: small Providers do not need empty safety, validation, or
# architecture documents merely to match larger packages.
$providerRoot = Join-Path $workspace 'providers'
if (Test-Path -LiteralPath $providerRoot -PathType Container) {
    foreach ($providerDirectory in Get-ChildItem -LiteralPath $providerRoot -Directory) {
        $providerReadmePath = Join-Path $providerDirectory.FullName 'README.md'
        $providerDocuments = @(
            $markdownFiles | Where-Object {
                $_.FullName.StartsWith(
                    $providerDirectory.FullName + [System.IO.Path]::DirectorySeparatorChar,
                    [System.StringComparison]::OrdinalIgnoreCase
                )
            }
        )
        if ($providerDocuments.Count -eq 0) {
            continue
        }
        if (-not (Test-Path -LiteralPath $providerReadmePath -PathType Leaf)) {
            $relativeProvider = $providerDirectory.FullName.Substring($workspace.Length).TrimStart('\', '/')
            $failures.Add("${relativeProvider}: Provider documents have no README.md landing page")
            continue
        }

        $directTargets = [System.Collections.Generic.HashSet[string]]::new(
            [System.StringComparer]::OrdinalIgnoreCase
        )
        foreach ($line in Get-Content -LiteralPath $providerReadmePath -Encoding utf8) {
            foreach ($match in [regex]::Matches($line, $linkPattern)) {
                $target = $match.Groups[1].Value.Trim()
                if ($target.StartsWith('<') -and $target.EndsWith('>')) {
                    $target = $target.Substring(1, $target.Length - 2)
                }
                else {
                    $target = ($target -split '\s+"', 2)[0]
                }
                if ($target -match '^(?i:https?://|mailto:|app://|data:|#)') {
                    continue
                }
                $target = ($target -split '#', 2)[0]
                if (-not $target) {
                    continue
                }
                try {
                    $decodedTarget = [uri]::UnescapeDataString($target)
                    $resolvedTarget = [System.IO.Path]::GetFullPath(
                        (Join-Path $providerDirectory.FullName ($decodedTarget -replace '/', '\'))
                    )
                    [void]$directTargets.Add($resolvedTarget)
                }
                catch {
                    # The general link validator reports the actionable error.
                }
            }
        }

        foreach ($providerDocument in $providerDocuments) {
            if ($providerDocument.FullName -eq $providerReadmePath) {
                continue
            }
            if (-not $directTargets.Contains($providerDocument.FullName)) {
                $relativeDocument = $providerDocument.FullName.Substring($workspace.Length).TrimStart('\', '/')
                $relativeReadme = $providerReadmePath.Substring($workspace.Length).TrimStart('\', '/')
                $failures.Add("${relativeDocument}: not linked directly from $relativeReadme")
            }
        }
    }
}

$stalePatterns = @(
    @{ Pattern = 'docs[\\/]reference[\\/]project_notes'; Message = 'retired project-note path' },
    @{ Pattern = 'project_docs[\\/]'; Message = 'retired project_docs path' },
    @{ Pattern = 'C:\\Projects\\testing_physical_ai'; Message = 'developer-specific workspace path' },
    @{ Pattern = 'docs[\\/]04_MAIN_GUI_PORTAL\.md'; Message = 'retired portal document path' },
    @{ Pattern = 'docs[\\/]11_VERSION_HISTORY_AND_DECISIONS\.md'; Message = 'retired version-history document path' },
    @{ Pattern = 'docs[\\/]12_FOUNDATIONPOSE_OBJECT_POSE\.md'; Message = 'retired FoundationPose guide path' },
    @{ Pattern = 'providers[\\/]orbbec_femto_bolt[\\/]docs[\\/](sdk_distribution|OFFICIAL_REFERENCES|DEVICE_CONTROL_ROADMAP)\.md'; Message = 'retired Orbbec document path' },
    @{ Pattern = 'providers[\\/]rebot_arm_dm[\\/]docs[\\/]CALIBRATION_(MATH|WORKFLOW)\.md'; Message = 'retired Basic calibration document path' },
    @{ Pattern = 'providers[\\/]rebot_arm_integrated[\\/]docs[\\/](FABRIC_COMMAND|UPSTREAM_DISCOVERY|XBOX_MAPPING)\.md'; Message = 'retired Integrated document path' },
    @{ Pattern = 'Neither position nor orientation residual rejects the physical action'; Message = 'stale Integrated IK-residual behavior' },
    @{ Pattern = 'tool-to-acting-point'; Message = 'ambiguous Integrated frame terminology; use tool-to-controlled-frame' },
    @{ Pattern = 'controlled/acting-point'; Message = 'ambiguous Integrated frame terminology; use controlled frame' }
)

foreach ($file in $markdownFiles) {
    $relativePath = $file.FullName.Substring($workspace.Length).TrimStart('\', '/')
    $lineNumber = 0
    foreach ($line in Get-Content -LiteralPath $file.FullName -Encoding utf8) {
        $lineNumber++
        foreach ($entry in $stalePatterns) {
            if ($line -match $entry.Pattern) {
                $failures.Add("${relativePath}:${lineNumber}: $($entry.Message)")
            }
        }
    }
}

$requiredDocs = @(
    'README.md',
    'docs\README.md',
    'docs\01_ARCHITECTURE_AND_DATA_FLOW.md',
    'docs\03_SETUP_AND_OPERATION.md',
    'docs\05_COMPATIBILITY_AND_EXTENSION.md',
    'docs\06_VALIDATION.md',
    'docs\07_CONFIGURATION_AND_SECURITY.md',
    'docs\09_LIMITATIONS_AND_ROADMAP.md',
    'docs\13_GRIPPER_MOTION_ARM_ROOT_ALIGNMENT.md',
    'contracts\README.md'
)
foreach ($requiredDoc in $requiredDocs) {
    if (-not (Test-Path -LiteralPath (Join-Path $workspace $requiredDoc) -PathType Leaf)) {
        $failures.Add("Missing required active document: $requiredDoc")
    }
}

if ($failures.Count -gt 0) {
    $failures | Sort-Object -Unique | ForEach-Object { Write-Error $_ }
    throw "Documentation validation failed with $($failures.Count) issue(s)."
}

Write-Host "Documentation validation passed: $($markdownFiles.Count) Markdown files."
