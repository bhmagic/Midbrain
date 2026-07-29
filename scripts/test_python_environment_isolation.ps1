$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$workspace = Split-Path $PSScriptRoot -Parent
$requiredSetupScripts = @(
    "providers\foundation_pose\scripts\setup.ps1",
    "providers\local_vio\scripts\setup.ps1",
    "providers\orbbec_femto_bolt\scripts\setup.ps1",
    "providers\rebot_arm_dm\scripts\setup.ps1",
    "providers\rebot_arm_integrated\scripts\setup.ps1",
    "skills\locate-effector-front\scripts\setup.ps1",
    "skills\register_tool_to_control_frame\scripts\setup.ps1",
    "skills\spatial_registration_rgbd\scripts\setup.ps1",
    "skills\stationary_world_arm_alignment\scripts\setup.ps1",
    "skills\vegetable_cutting\scripts\setup.ps1",
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
