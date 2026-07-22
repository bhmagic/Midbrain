param(
    [Parameter(Mandatory = $true)]
    [string]$InputObj,

    [Parameter(Mandatory = $true)]
    [string]$ModelId,

    [ValidateSet("robot_base", "robot_gripper", "generic_object")]
    [string]$Role = "generic_object",

    [Parameter(Mandatory = $true)]
    [string]$SemanticFrame,

    [string]$DefaultChildFrame = "",

    [string]$Description = "",

    [ValidateSet("millimeters", "centimeters", "meters")]
    [string]$CoordinateUnits = "millimeters",

    [double]$ScaleToM = 0.001,

    [double]$MergeDistance = 0.001,

    [ValidateSet("centered_mesh", "original_export", "custom")]
    [string]$SemanticFrameMode = "centered_mesh",

    [string]$MeshFromSemanticJson = "",

    [string]$BlenderExe = "C:\Program Files\Blender Foundation\Blender 5.0\blender.exe",

    [string]$Workspace = "",

    [string]$OutputName = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$toolRoot = $PSScriptRoot
$providerRoot = (Resolve-Path (Join-Path $toolRoot "..\..")).Path

if ([string]::IsNullOrWhiteSpace($Workspace)) {
    $Workspace = (Resolve-Path (Join-Path $providerRoot "..\..")).Path
}

if ([string]::IsNullOrWhiteSpace($OutputName)) {
    $OutputName = $ModelId
}

if ($ScaleToM -le 0) {
    throw "ScaleToM must be positive."
}

if ($SemanticFrameMode -eq "custom" -and [string]::IsNullOrWhiteSpace($MeshFromSemanticJson)) {
    throw "SemanticFrameMode 'custom' requires -MeshFromSemanticJson."
}

if ($SemanticFrameMode -ne "custom" -and -not [string]::IsNullOrWhiteSpace($MeshFromSemanticJson)) {
    throw "Use -SemanticFrameMode custom when supplying -MeshFromSemanticJson."
}

$input = (Resolve-Path $InputObj).Path

if (-not (Test-Path -LiteralPath $BlenderExe -PathType Leaf)) {
    throw "Blender executable was not found: $BlenderExe"
}

$configRoot = Join-Path $Workspace "config\foundation_pose"
$modelRoot = Join-Path $configRoot "models"
$sourceRoot = Join-Path $configRoot "source"
$registry = Join-Path $configRoot "models.json"

New-Item -ItemType Directory -Force -Path $modelRoot | Out-Null
New-Item -ItemType Directory -Force -Path $sourceRoot | Out-Null

$sourceCopy = Join-Path $sourceRoot ([System.IO.Path]::GetFileName($input))
Copy-Item -LiteralPath $input -Destination $sourceCopy -Force

& $BlenderExe `
    --background `
    --python (Join-Path $toolRoot "prepare_mesh_blender.py") `
    -- `
    --input $sourceCopy `
    --output-dir $modelRoot `
    --name $OutputName `
    --merge-distance $MergeDistance `
    --coordinate-units $CoordinateUnits `
    --scale-to-m $ScaleToM

if ($LASTEXITCODE -ne 0) {
    throw "Blender mesh preparation failed with exit code $LASTEXITCODE."
}

$centeredMesh = Join-Path $modelRoot "${OutputName}_clean_centered.obj"
$metadataPath = Join-Path $modelRoot "${OutputName}_mesh_metadata.json"

$python = Join-Path $providerRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    $python = "py"
    $pythonArgs = @("-3.11")
}
else {
    $pythonArgs = @()
}

$temporaryTransform = $null
$effectiveTransformJson = ""

try {
    if ($SemanticFrameMode -eq "original_export") {
        $metadata = Get-Content -LiteralPath $metadataPath -Raw | ConvertFrom-Json
        $translation = @(
            $metadata.center_transform_in_original_obj_axes.centered_from_original_translation_input_units
        )

        if ($translation.Count -ne 3) {
            throw "Preparation metadata did not contain a three-value centering translation."
        }

        $tx = [double]$translation[0] * $ScaleToM
        $ty = [double]$translation[1] * $ScaleToM
        $tz = [double]$translation[2] * $ScaleToM

        $temporaryTransform = Join-Path `
            $env:TEMP `
            ("foundation_pose_mesh_from_semantic_" + [guid]::NewGuid().ToString("N") + ".json")

        $transformDocument = [ordered]@{
            mesh_from_semantic = @(
                1.0, 0.0, 0.0, $tx,
                0.0, 1.0, 0.0, $ty,
                0.0, 0.0, 1.0, $tz,
                0.0, 0.0, 0.0, 1.0
            )
        }

        [System.IO.File]::WriteAllText(
            $temporaryTransform,
            (($transformDocument | ConvertTo-Json -Depth 5) + [Environment]::NewLine),
            [System.Text.UTF8Encoding]::new($false)
        )

        $effectiveTransformJson = $temporaryTransform
    }
    elseif ($SemanticFrameMode -eq "custom") {
        $effectiveTransformJson = (Resolve-Path $MeshFromSemanticJson).Path
    }

    $arguments = @(
        (Join-Path $toolRoot "generate_registry_entry.py"),
        "--registry", $registry,
        "--model-id", $ModelId,
        "--role", $Role,
        "--description", $Description,
        "--mesh-path", $centeredMesh,
        "--semantic-frame", $SemanticFrame,
        "--scale-to-m", "$ScaleToM",
        "--revision", "cad-prepared",
        "--replace"
    )

    if (-not [string]::IsNullOrWhiteSpace($DefaultChildFrame)) {
        $arguments += @("--default-child-frame", $DefaultChildFrame)
    }

    if (-not [string]::IsNullOrWhiteSpace($effectiveTransformJson)) {
        $arguments += @(
            "--mesh-from-semantic-json",
            $effectiveTransformJson
        )
    }

    & $python @pythonArgs @arguments

    if ($LASTEXITCODE -ne 0) {
        throw "Registry generation failed with exit code $LASTEXITCODE."
    }
}
finally {
    if ($null -ne $temporaryTransform -and (Test-Path -LiteralPath $temporaryTransform)) {
        Remove-Item -LiteralPath $temporaryTransform -Force
    }
}

Write-Host ""
Write-Host "Prepared FoundationPose model:"
Write-Host "  Source copy:          $sourceCopy"
Write-Host "  Centered mesh:        $centeredMesh"
Write-Host "  Metadata:             $metadataPath"
Write-Host "  Registry:             $registry"
Write-Host "  Semantic frame mode:  $SemanticFrameMode"
Write-Host ""
Write-Host "Review the generated model entry before treating its reporting frame as a robot kinematic frame."
