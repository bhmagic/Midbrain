param(
    [string]$ProviderRoot = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$TempRoot = Join-Path `
    $env:TEMP `
    ("foundation_pose_seed_regression_" + [guid]::NewGuid().ToString("N"))

$ConfigRoot = Join-Path $TempRoot "config\foundation_pose"
$RegistryPath = Join-Path $ConfigRoot "models.json"
$PreviousRoot = $env:PHYSICAL_AGENT_ROOT

function Write-Utf8NoBom {
    param(
        [string]$Path,
        [string]$Text
    )

    [System.IO.File]::WriteAllText(
        $Path,
        $Text,
        [System.Text.UTF8Encoding]::new($false)
    )
}

try {
    New-Item -ItemType Directory -Force -Path $ConfigRoot | Out-Null

    $Existing = [ordered]@{
        revision = "rebot-b601-dm-foundationpose-v2"
        models = @(
            [ordered]@{
                model_id = "robot_arm_root"
                mesh_path = "models/Base_clean_centered.obj"
                semantic_frame = "robot/arm_root"
                mesh_from_semantic = @(
                    1.0, 0.0, 0.0, 0.0,
                    0.0, 1.0, 0.0, 0.0,
                    0.0, 0.0, 1.0, -0.0446249945,
                    0.0, 0.0, 0.0, 1.0
                )
                scale_to_m = 0.001
                symmetry = [ordered]@{
                    type = "NONE"
                }
                enabled = $true
                revision = "rebot-b601-dm-base-v1"
            },
            [ordered]@{
                model_id = "robot_gripper_slider_support"
                mesh_path = "models/Gripper_clean_centered.obj"
                semantic_frame = "robot/gripper_slider_support_center"
                mesh_from_semantic = @(
                    1.0, 0.0, 0.0, 0.0,
                    0.0, 1.0, 0.0, 0.0,
                    0.0, 0.0, 1.0, 0.0,
                    0.0, 0.0, 0.0, 1.0
                )
                scale_to_m = 0.001
                symmetry = [ordered]@{
                    type = "NONE"
                }
                enabled = $true
                revision = "rebot-b601-dm-gripper-slider-support-v1"
            }
        )
    }

    Write-Utf8NoBom `
        -Path $RegistryPath `
        -Text (($Existing | ConvertTo-Json -Depth 30) + [Environment]::NewLine)

    $env:PHYSICAL_AGENT_ROOT = $TempRoot

    & (Join-Path $ProviderRoot "scripts\seed_default_models.ps1")

    $Result = Get-Content `
        -LiteralPath $RegistryPath `
        -Raw |
        ConvertFrom-Json

    $Models = @($Result.models)

    if ($Models.Count -ne 2) {
        throw "Expected exactly 2 models after migration; got $($Models.Count)."
    }

    $Base = @(
        $Models |
        Where-Object { $_.model_id -eq "robot_arm_root" }
    )

    $Gripper = @(
        $Models |
        Where-Object {
            $_.model_id -eq "robot_gripper_slider_support"
        }
    )

    if ($Base.Count -ne 1) {
        throw "Expected one robot_arm_root model after migration."
    }

    if ($Gripper.Count -ne 1) {
        throw "Expected one robot_gripper_slider_support model after migration."
    }

    if ($Base[0].role -ne "robot_base") {
        throw "Base role metadata was not migrated."
    }

    if ($Gripper[0].role -ne "robot_gripper") {
        throw "Gripper role metadata was not migrated."
    }

    if ($Base[0].mesh_path -ne "models/Base_clean_centered.obj") {
        throw "Base mesh_path changed unexpectedly."
    }

    if (
        $Gripper[0].mesh_path -ne
        "models/Gripper_clean_centered.obj"
    ) {
        throw "Gripper mesh_path changed unexpectedly."
    }

    $BaseTransform = @($Base[0].mesh_from_semantic)
    $GripperTransform = @($Gripper[0].mesh_from_semantic)

    if ($BaseTransform.Count -ne 16) {
        throw "Base mesh_from_semantic length changed unexpectedly."
    }

    if (
        [math]::Abs(
            [double]$BaseTransform[11] - (-0.0446249945)
        ) -gt 1e-9
    ) {
        throw "Base semantic centering translation changed unexpectedly."
    }

    if ($GripperTransform.Count -ne 16) {
        throw "Gripper mesh_from_semantic length changed unexpectedly."
    }

    if (
        [math]::Abs([double]$GripperTransform[3]) -gt 1e-12 -or
        [math]::Abs([double]$GripperTransform[7]) -gt 1e-12 -or
        [math]::Abs([double]$GripperTransform[11]) -gt 1e-12
    ) {
        throw "Gripper identity reporting transform changed unexpectedly."
    }

    foreach ($RequiredFile in @(
        (Join-Path $ConfigRoot "models\Base_clean_centered.obj"),
        (Join-Path $ConfigRoot "models\Gripper_clean_centered.obj")
    )) {
        if (-not (
            Test-Path `
                -LiteralPath $RequiredFile `
                -PathType Leaf
        )) {
            throw "Default geometry was not seeded: $RequiredFile"
        }
    }

    Write-Host "[PASS] Existing Base+Gripper registry migration"
}
finally {
    $env:PHYSICAL_AGENT_ROOT = $PreviousRoot

    Remove-Item `
        -LiteralPath $TempRoot `
        -Recurse `
        -Force `
        -ErrorAction SilentlyContinue
}
