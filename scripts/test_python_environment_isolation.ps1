$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$workspace = Split-Path $PSScriptRoot -Parent
$requiredSetupScripts = @(
    "providers\foundation_pose\scripts\setup.ps1",
    "providers\arm_scene_compiler\scripts\setup.ps1",
    "providers\sam2_scene_tracker\scripts\setup.ps1",
    "providers\local_vio\scripts\setup.ps1",
    "providers\orbbec_femto_bolt\scripts\setup.ps1",
    "providers\rebot_arm_dm\scripts\setup.ps1",
    "providers\rebot_arm_integrated\scripts\setup.ps1",
    "providers\rebot_arm_contact\scripts\setup.ps1",
    "skills\contact_work_runtime\scripts\setup.ps1",
    "skills\slicing\scripts\setup.ps1",
    "skills\limited-graph\scripts\setup.ps1",
    "skills\locate-effector-front\scripts\setup.ps1",
    "skills\register_tool_to_control_frame\scripts\setup.ps1",
    "skills\refine-arm-root-translation\scripts\setup.ps1",
    "skills\spatial_registration_rgbd\scripts\setup.ps1",
    "skills\locate_arm_base\scripts\setup.ps1",
    "test_agent\scripts\setup.ps1"
)

foreach ($relativePath in $requiredSetupScripts) {
    $setupPath = Join-Path $workspace $relativePath
    if (-not (Test-Path -LiteralPath $setupPath -PathType Leaf)) {
        throw "Python component setup script is missing: $relativePath"
    }
    $setupText = Get-Content -Raw -LiteralPath $setupPath
    if ($setupText -notmatch '\.venv') {
        throw "Python component setup does not declare a local .venv: $relativePath"
    }
}

$skillSetupFiles = Get-ChildItem -LiteralPath (Join-Path $workspace "skills") `
    -Recurse -File -Filter "setup.ps1"
foreach ($setupFile in $skillSetupFiles) {
    $setupText = Get-Content -Raw -LiteralPath $setupFile.FullName
    if ($setupText -match 'providers[\\/]') {
        throw "Skill setup installs or references a Provider implementation: $($setupFile.FullName)"
    }
}

$boundaryFiles = @(
    "providers\foundation_pose\python\foundation_pose_provider",
    "providers\sam2_scene_tracker\python\sam2_scene_tracker",
    "skills\locate_arm_base\python\locate_arm_base"
)
foreach ($relativePath in $boundaryFiles) {
    $sourceFiles = Get-ChildItem -LiteralPath (Join-Path $workspace $relativePath) `
        -Recurse -File -Filter "*.py"
    foreach ($sourceFile in $sourceFiles) {
        $sourceText = Get-Content -Raw -LiteralPath $sourceFile.FullName
        if ($sourceText -match '(?m)^\s*(from|import)\s+orbbec_femto_provider\b') {
            throw "Foundation localization boundary imports the camera Provider: $($sourceFile.FullName)"
        }
    }
}

$providerBoundaryRules = @(
    @{
        Path = "providers\foundation_pose\python\foundation_pose_provider"
        Forbidden = "sam2|sam2_scene_tracker|locate_arm_base|torch"
    },
    @{
        Path = "providers\sam2_scene_tracker\python\sam2_scene_tracker"
        Forbidden = "foundation_pose_provider|locate_arm_base|tensorrt"
    }
)
foreach ($rule in $providerBoundaryRules) {
    $providerSource = Join-Path $workspace $rule.Path
    foreach ($sourceFile in Get-ChildItem -LiteralPath $providerSource -Recurse -File -Filter "*.py") {
        $sourceText = Get-Content -Raw -LiteralPath $sourceFile.FullName
        $pattern = "(?m)^\s*(from|import)\s+(" + $rule.Forbidden + ")\b"
        if ($sourceText -match $pattern) {
            throw "Foundation localization Provider imports another implementation: $($sourceFile.FullName)"
        }
    }
}

$skillSource = Join-Path $workspace "skills\locate_arm_base\python\locate_arm_base"
foreach ($sourceFile in Get-ChildItem -LiteralPath $skillSource -Recurse -File -Filter "*.py") {
    $sourceText = Get-Content -Raw -LiteralPath $sourceFile.FullName
    if ($sourceText -match '(?m)^\s*(from|import)\s+(foundation_pose_provider|sam2|sam2_scene_tracker|tensorrt|torch)\b') {
        throw "Locate Arm Base Skill imports a Provider implementation: $($sourceFile.FullName)"
    }
}

$operationalFiles = @(
    Get-ChildItem -LiteralPath (Join-Path $workspace "platform_core\scripts") -File -Filter "*.ps1"
    Get-ChildItem -LiteralPath (Join-Path $workspace "providers") -Recurse -File |
        Where-Object {
            $_.FullName -match '[\\/]scripts[\\/].*\.ps1$' -or
            $_.Name -eq "provider_entry.json"
        }
    Get-ChildItem -LiteralPath (Join-Path $workspace "skills") -Recurse -File |
        Where-Object { $_.FullName -match '[\\/]scripts[\\/].*\.ps1$' }
    Get-ChildItem -LiteralPath (Join-Path $workspace "test_agent\scripts") -File -Filter "*.ps1"
    Get-Item -LiteralPath (Join-Path $workspace "config\providers.json.example")
    Get-Item -LiteralPath (
        Join-Path $workspace "platform_core\config_templates\providers.json.example"
    )
)

foreach ($file in $operationalFiles) {
    $text = Get-Content -Raw -LiteralPath $file.FullName
    if ($text.Contains('${PHYSICAL_AGENT_PYTHON}')) {
        throw "Shared interpreter placeholder remains in $($file.FullName)"
    }
    if (
        $text.Contains('Join-Path $workspace ".venv') -or
        $text.Contains('Join-Path $WorkspaceRoot ".venv')
    ) {
        throw "Repository-root virtual environment reference remains in $($file.FullName)"
    }
}

Write-Host "Python component environment isolation checks passed."
