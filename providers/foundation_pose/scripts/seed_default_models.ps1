param(
    [switch]$ForceRegistry
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

. (Join-Path $PSScriptRoot "common.ps1")

$provider = Get-ProviderRoot
$workspace = Get-WorkspaceRoot

$seedRoot = Join-Path $provider "defaults\rebot_b601_dm"
$configRoot = Join-Path $workspace "config\foundation_pose"
$modelRoot = Join-Path $configRoot "models"
$sourceRoot = Join-Path $configRoot "source"
$licenseRoot = Join-Path $configRoot "licenses"
$registry = Join-Path $configRoot "models.json"
$seedRegistry = Join-Path $seedRoot "models.json"

foreach ($directory in @(
    $configRoot,
    $modelRoot,
    $sourceRoot,
    $licenseRoot
)) {
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
}

function Copy-IfMissing {
    param(
        [string]$Source,
        [string]$Destination
    )

    if (Test-Path -LiteralPath $Destination -PathType Leaf) {
        Write-Host "[PRESERVED] $Destination"
        return
    }

    Copy-Item -LiteralPath $Source -Destination $Destination
    Write-Host "[SEEDED] $Destination"
}

foreach ($name in @(
    "Base_clean_centered.obj",
    "Base_clean_original_frame.obj",
    "Base_mesh_metadata.json",
    "Gripper_clean_centered.obj",
    "Gripper_clean_original_frame.obj",
    "Gripper_mesh_metadata.json"
)) {
    Copy-IfMissing `
        -Source (Join-Path $seedRoot "models\$name") `
        -Destination (Join-Path $modelRoot $name)
}

foreach ($name in @(
    "Base.obj",
    "Gripper.obj",
    "01_BASE_Plate.step",
    "01_BASE_Link.step",
    "01_Rail_Bracket.step"
)) {
    Copy-IfMissing `
        -Source (Join-Path $seedRoot "source\$name") `
        -Destination (Join-Path $sourceRoot $name)
}

Copy-IfMissing `
    -Source (Join-Path $seedRoot "licenses\CERN-OHL-W-2.0.txt") `
    -Destination (Join-Path $licenseRoot "reBot-CERN-OHL-W-2.0.txt")

Copy-IfMissing `
    -Source (Join-Path $seedRoot "UPSTREAM.md") `
    -Destination (Join-Path $configRoot "rebot_UPSTREAM.md")

Copy-IfMissing `
    -Source (Join-Path $seedRoot "MODIFICATIONS.md") `
    -Destination (Join-Path $configRoot "rebot_MODIFICATIONS.md")

if ($ForceRegistry -or -not (Test-Path -LiteralPath $registry -PathType Leaf)) {
    Copy-Item `
        -LiteralPath $seedRegistry `
        -Destination $registry `
        -Force

    Write-Host "[SEEDED] $registry"
}
else {
    $existing = Get-Content -LiteralPath $registry -Raw | ConvertFrom-Json
    $defaults = Get-Content -LiteralPath $seedRegistry -Raw | ConvertFrom-Json

    if ($null -eq $existing.models) {
        throw "Existing FoundationPose models.json does not contain a models array."
    }

    $existingModels = @($existing.models)
    $existingIds = @($existingModels | ForEach-Object { [string]$_.model_id })
    $existingProfileId = ""
    $existingRevision = ""

    if ($null -ne $existing.PSObject.Properties["profile"] -and $null -ne $existing.profile) {
        $existingProfileId = [string]$existing.profile.id
    }

    if ($null -ne $existing.PSObject.Properties["revision"]) {
        $existingRevision = [string]$existing.revision
    }

    $looksLikeRebot = (
        $existingProfileId -eq "rebot_b601_dm" -or
        $existingRevision.StartsWith("rebot-b601-dm-") -or
        ($existingIds -contains "robot_arm_root") -or
        ($existingIds -contains "robot_gripper_slider_support")
    )

    if (-not $looksLikeRebot) {
        Write-Host "[PRESERVED] Custom model registry was not modified: $registry"
    }
    else {
        # Preserve existing geometry/frame transforms. Refresh only publication
        # metadata that identifies the default reporter roles and stable child
        # frames, and add a missing default reporter when necessary.
        $merged = New-Object System.Collections.Generic.List[object]

        foreach ($defaultModel in @($defaults.models)) {
            $matches = @(
                $existingModels |
                Where-Object { $_.model_id -eq $defaultModel.model_id }
            )

            if ($matches.Count -eq 0) {
                $merged.Add($defaultModel)
                Write-Host "[ADDED DEFAULT MODEL] $($defaultModel.model_id)"
                continue
            }

            $model = $matches[0]
            $model | Add-Member -MemberType NoteProperty -Name role -Value $defaultModel.role -Force
            $model | Add-Member -MemberType NoteProperty -Name description -Value $defaultModel.description -Force
            $model | Add-Member -MemberType NoteProperty -Name default_child_frame -Value $defaultModel.default_child_frame -Force
            $merged.Add($model)
            Write-Host "[REFRESHED METADATA] $($model.model_id) role=$($defaultModel.role)"
        }

        foreach ($model in $existingModels) {
            if (@($defaults.models | Where-Object { $_.model_id -eq $model.model_id }).Count -eq 0) {
                $merged.Add($model)
            }
        }

        $existing.models = [object[]]$merged.ToArray()
        $existing | Add-Member -MemberType NoteProperty -Name profile -Value $defaults.profile -Force
        $existing | Add-Member -MemberType NoteProperty -Name revision -Value $defaults.revision -Force

        $json = $existing | ConvertTo-Json -Depth 50
        [System.IO.File]::WriteAllText(
            $registry,
            $json + [Environment]::NewLine,
            [System.Text.UTF8Encoding]::new($false)
        )

        Write-Host "[MIGRATED DEFAULT PROFILE METADATA] $registry"
    }
}

Write-Host ""
Write-Host "Default FoundationPose profile: reBot B601-DM"
Write-Host "Persistent config: $configRoot"
