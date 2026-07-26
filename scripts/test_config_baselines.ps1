[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$workspace = Split-Path $PSScriptRoot -Parent
$failures = [System.Collections.Generic.List[string]]::new()

function Add-Failure {
    param([Parameter(Mandatory = $true)][string]$Message)
    $failures.Add($Message)
}

function Assert-True {
    param(
        [Parameter(Mandatory = $true)][bool]$Condition,
        [Parameter(Mandatory = $true)][string]$Message
    )
    if (-not $Condition) {
        Add-Failure $Message
    }
}

function Get-NormalizedText {
    param([Parameter(Mandatory = $true)][string]$Path)
    return ([System.IO.File]::ReadAllText($Path)).Replace("`r`n", "`n")
}

function Get-CanonicalJson {
    param([Parameter(Mandatory = $true)][string]$Path)
    return (Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json | ConvertTo-Json -Depth 100 -Compress)
}

function Read-EnvTemplate {
    param([Parameter(Mandatory = $true)][string]$Path)
    $values = @{}
    foreach ($line in Get-Content -LiteralPath $Path) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) {
            continue
        }
        $parts = $trimmed.Split("=", 2)
        if ($parts.Count -ne 2 -or -not $parts[0].Trim()) {
            Add-Failure "Malformed environment line in ${Path}: $line"
            continue
        }
        $name = $parts[0].Trim()
        if ($values.ContainsKey($name)) {
            Add-Failure "Duplicate environment key $name in $Path"
            continue
        }
        $values[$name] = $parts[1]
    }
    return $values
}

function Assert-FileContains {
    param(
        [Parameter(Mandatory = $true)][string]$RelativePath,
        [Parameter(Mandatory = $true)][string[]]$Tokens
    )
    $content = Get-Content -LiteralPath (Join-Path $workspace $RelativePath) -Raw
    foreach ($token in $Tokens) {
        Assert-True `
            -Condition $content.Contains($token) `
            -Message "$RelativePath no longer contains the expected generation contract token: $token"
    }
}

$requiredFiles = @(
    "config/README.md",
    "config/BASELINE_INVENTORY.md",
    "config/system.env.example",
    "config/api_keys.env.example",
    "config/providers.json.example",
    "platform_core/config_templates/api_keys.env.example",
    "platform_core/config_templates/system.env.example",
    "platform_core/config_templates/providers.json.example",
    "test_agent/config_templates/system.env.example",
    "test_agent/config_templates/api_keys.env.example",
    "providers/orbbec_femto_bolt/config_templates/provider_entry.json",
    "providers/local_vio/config_templates/provider_entry.json",
    "providers/foundation_pose/config_templates/provider_entry.json",
    "providers/rebot_arm_dm/config_templates/provider_entry.json",
    "providers/rebot_arm_dm/config_templates/arm_model.factory.json",
    "providers/rebot_arm_dm/config_templates/arm_calibration.initial.json",
    "providers/rebot_arm_dm/config_templates/calibration_collision_model.json",
    "providers/rebot_arm_integrated/config_templates/provider_entry.json",
    "providers/rebot_arm_integrated/config_templates/controller.default.json",
    "skills/stationary_world_arm_alignment/config_templates/alignment.default.json",
    "config/foundation_pose/models.json",
    "providers/rebot_arm_dm/scripts/setup.ps1",
    "providers/rebot_arm_integrated/scripts/setup.ps1",
    "providers/rebot_arm_integrated/python/rebot_arm_integrated/config_repair.py",
    "providers/foundation_pose/scripts/seed_default_models.ps1",
    "providers/orbbec_femto_bolt/python/orbbec_femto_provider/device_calibration.py",
    "skills/stationary_world_arm_alignment/python/stationary_world_arm_alignment/config.py",
    "test_agent/scripts/setup.ps1"
)

foreach ($relativePath in $requiredFiles) {
    Assert-True `
        -Condition (Test-Path -LiteralPath (Join-Path $workspace $relativePath) -PathType Leaf) `
        -Message "Missing required clean configuration source: $relativePath"
}

if ($failures.Count -gt 0) {
    throw "Configuration baseline audit failed before parsing:`n$($failures -join "`n")"
}

$jsonFiles = @(
    "config/providers.json.example",
    "platform_core/config_templates/providers.json.example",
    "providers/orbbec_femto_bolt/config_templates/provider_entry.json",
    "providers/local_vio/config_templates/provider_entry.json",
    "providers/foundation_pose/config_templates/provider_entry.json",
    "providers/rebot_arm_dm/config_templates/provider_entry.json",
    "providers/rebot_arm_dm/config_templates/arm_model.factory.json",
    "providers/rebot_arm_dm/config_templates/arm_calibration.initial.json",
    "providers/rebot_arm_dm/config_templates/calibration_collision_model.json",
    "providers/rebot_arm_integrated/config_templates/provider_entry.json",
    "providers/rebot_arm_integrated/config_templates/controller.default.json",
    "skills/stationary_world_arm_alignment/config_templates/alignment.default.json",
    "config/foundation_pose/models.json"
)
foreach ($relativePath in $jsonFiles) {
    try {
        Get-Content -LiteralPath (Join-Path $workspace $relativePath) -Raw | ConvertFrom-Json | Out-Null
    }
    catch {
        Add-Failure "Invalid JSON in ${relativePath}: $($_.Exception.Message)"
    }
}

$rootSystem = Join-Path $workspace "config/system.env.example"
$coreSystem = Join-Path $workspace "platform_core/config_templates/system.env.example"
$agentSystem = Join-Path $workspace "test_agent/config_templates/system.env.example"
Assert-True `
    -Condition ((Get-NormalizedText $rootSystem) -ceq (Get-NormalizedText $coreSystem)) `
    -Message "Root and platform-core system.env templates have drifted"
Assert-True `
    -Condition ((Get-NormalizedText $rootSystem) -ceq (Get-NormalizedText $agentSystem)) `
    -Message "Root and Test Agent system.env templates have drifted"

$rootApiKeys = Join-Path $workspace "config/api_keys.env.example"
$coreApiKeys = Join-Path $workspace "platform_core/config_templates/api_keys.env.example"
$agentApiKeys = Join-Path $workspace "test_agent/config_templates/api_keys.env.example"
Assert-True `
    -Condition ((Get-NormalizedText $rootApiKeys) -ceq (Get-NormalizedText $coreApiKeys)) `
    -Message "Root and platform-core API-key templates have drifted"
Assert-True `
    -Condition ((Get-NormalizedText $rootApiKeys) -ceq (Get-NormalizedText $agentApiKeys)) `
    -Message "Root and Test Agent API-key templates have drifted"

$rootProviders = Join-Path $workspace "config/providers.json.example"
$coreProviders = Join-Path $workspace "platform_core/config_templates/providers.json.example"
Assert-True `
    -Condition ((Get-CanonicalJson $rootProviders) -ceq (Get-CanonicalJson $coreProviders)) `
    -Message "Root and platform-core Provider templates have drifted"

$providerDocument = Get-Content -LiteralPath $rootProviders -Raw | ConvertFrom-Json
$providerEntries = @($providerDocument.providers)
Assert-True -Condition ($providerEntries.Count -eq 2) -Message "Clean Provider baseline must contain camera and Local VIO entries"
$expectedProviderTemplates = @{
    "camera.femto_bolt" = "providers/orbbec_femto_bolt/config_templates/provider_entry.json"
    "localization.local_vio" = "providers/local_vio/config_templates/provider_entry.json"
}
foreach ($providerId in $expectedProviderTemplates.Keys) {
    $matches = @($providerEntries | Where-Object { $_.id -eq $providerId })
    Assert-True -Condition ($matches.Count -eq 1) -Message "Clean Provider baseline must contain exactly one $providerId entry"
    if ($matches.Count -eq 1) {
        $actual = $matches[0] | ConvertTo-Json -Depth 100 -Compress
        $expected = Get-CanonicalJson (Join-Path $workspace $expectedProviderTemplates[$providerId])
        Assert-True -Condition ($actual -ceq $expected) -Message "Clean Provider entry $providerId differs from its package template"
    }
}

$allProviderTemplates = @(
    "providers/orbbec_femto_bolt/config_templates/provider_entry.json",
    "providers/local_vio/config_templates/provider_entry.json",
    "providers/foundation_pose/config_templates/provider_entry.json",
    "providers/rebot_arm_dm/config_templates/provider_entry.json",
    "providers/rebot_arm_integrated/config_templates/provider_entry.json"
)
$allProviderIds = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::Ordinal)
foreach ($relativePath in $allProviderTemplates) {
    $entry = Get-Content -LiteralPath (Join-Path $workspace $relativePath) -Raw | ConvertFrom-Json
    Assert-True -Condition $allProviderIds.Add([string]$entry.id) -Message "Duplicate Provider id in package templates: $($entry.id)"
    $arguments = @($entry.args)
    Assert-True -Condition ($arguments -contains '${MANAGER_URL}') -Message "$relativePath does not inherit MANAGER_URL from system.env"
    Assert-True -Condition ($arguments -contains '${FABRIC_URL}') -Message "$relativePath does not inherit FABRIC_URL from system.env"
}

$systemValues = Read-EnvTemplate $rootSystem
$requiredSystemKeys = @(
    "MANAGER_URL",
    "FABRIC_URL",
    "MANAGER_BIND",
    "FABRIC_BIND",
    "FABRIC_HISTORY_PER_STREAM",
    "FABRIC_TRANSFORM_HISTORY_PER_EDGE",
    "UI_HOST",
    "UI_PORT",
    "CAMERA_MAPPING_NAME",
    "HEAD_CAMERA_PROVIDER_ID",
    "LOCAL_VIO_PROVIDER_ID",
    "AUTO_INITIALIZE_SPACE_COGNITION",
    "SPACE_COGNITION_TIMEOUT_S",
    "POINT_CLOUD_RETENTION_S",
    "POINT_CLOUD_SAMPLE_STRIDE",
    "POINT_CLOUD_HZ",
    "POINT_CLOUD_MAX_POINTS",
    "FOUNDATION_POSE_CONTROL_URL"
)
foreach ($key in $requiredSystemKeys) {
    Assert-True -Condition $systemValues.ContainsKey($key) -Message "system.env.example is missing $key"
}

$apiValues = Read-EnvTemplate $rootApiKeys
foreach ($key in @("OPENAI_API_KEY", "GEMINI_API_KEY")) {
    Assert-True -Condition $apiValues.ContainsKey($key) -Message "api_keys.env.example is missing $key"
    if ($apiValues.ContainsKey($key)) {
        Assert-True -Condition ([string]::IsNullOrEmpty($apiValues[$key])) -Message "$key must be blank in api_keys.env.example"
    }
}
foreach ($key in @("OPENAI_AGENT_MODEL", "GEMINI_ROBOTICS_MODEL", "OPENAI_VISION_MODEL")) {
    Assert-True -Condition $apiValues.ContainsKey($key) -Message "api_keys.env.example is missing optional model selector $key"
}

$armCalibration = Get-Content -LiteralPath (Join-Path $workspace "providers/rebot_arm_dm/config_templates/arm_calibration.initial.json") -Raw | ConvertFrom-Json
Assert-True `
    -Condition ($armCalibration.calibration_revision -eq "UNVERIFIED-INITIAL") `
    -Message "Basic arm calibration baseline must remain explicitly unverified"
Assert-True `
    -Condition ($armCalibration.assembly_identity.serial -eq "UNASSIGNED") `
    -Message "Basic arm calibration baseline must not contain a real serial"

$integratedConfig = Get-Content -LiteralPath (Join-Path $workspace "providers/rebot_arm_integrated/config_templates/controller.default.json") -Raw | ConvertFrom-Json
Assert-True -Condition ($integratedConfig.schema_version -eq 3) -Message "Integrated clean controller template must use schema version 3"

$alignmentConfig = Get-Content -LiteralPath (Join-Path $workspace "skills/stationary_world_arm_alignment/config_templates/alignment.default.json") -Raw | ConvertFrom-Json
Assert-True `
    -Condition ($alignmentConfig.schema -eq "midbrain.skill.stationary_world_arm_alignment.config") `
    -Message "Stationary alignment default has the wrong schema"

$foundationRoot = Join-Path $workspace "config/foundation_pose"
$foundationRegistry = Get-Content -LiteralPath (Join-Path $foundationRoot "models.json") -Raw | ConvertFrom-Json
Assert-True -Condition (@($foundationRegistry.models).Count -gt 0) -Message "FoundationPose runtime registry contains no models"
foreach ($model in @($foundationRegistry.models)) {
    $meshPath = Join-Path $foundationRoot ([string]$model.mesh_path)
    Assert-True -Condition (Test-Path -LiteralPath $meshPath -PathType Leaf) -Message "FoundationPose model $($model.model_id) references missing mesh $($model.mesh_path)"
}
foreach ($relativePath in @(
    "references/Base_reference_atlas.json",
    "references/Base_reference_atlas.png",
    "references/Gripper_reference_atlas.json",
    "references/Gripper_reference_atlas.png"
)) {
    Assert-True -Condition (Test-Path -LiteralPath (Join-Path $foundationRoot $relativePath) -PathType Leaf) -Message "FoundationPose restore profile is missing $relativePath"
}

Assert-FileContains `
    -RelativePath "providers/rebot_arm_dm/scripts/setup.ps1" `
    -Tokens @("arm_model.factory.json", "arm_calibration.initial.json", "calibration_collision_model.json")
Assert-FileContains `
    -RelativePath "providers/rebot_arm_integrated/scripts/setup.ps1" `
    -Tokens @("controller.default.json", "controller.json")
Assert-FileContains `
    -RelativePath "providers/rebot_arm_integrated/python/rebot_arm_integrated/config_repair.py" `
    -Tokens @("controller.default.json", "active_path.parent.mkdir", "active_path.write_text")
Assert-FileContains `
    -RelativePath "providers/foundation_pose/scripts/seed_default_models.ps1" `
    -Tokens @("config\foundation_pose", "models.json", "Copy-IfMissing", "references", "CERN-OHL-W-2.0.txt", "UPSTREAM.md", "MODIFICATIONS.md")
Assert-FileContains `
    -RelativePath "providers/orbbec_femto_bolt/python/orbbec_femto_provider/device_calibration.py" `
    -Tokens @("_identity_document", "_atomic_json_write", "path.parent.mkdir")
Assert-FileContains `
    -RelativePath "skills/stationary_world_arm_alignment/python/stationary_world_arm_alignment/config.py" `
    -Tokens @("alignment.default.json", "path.mkdir(parents=True, exist_ok=True)")
Assert-FileContains `
    -RelativePath "test_agent/scripts/setup.ps1" `
    -Tokens @("api_keys.env.example", "system.env.example", "Test-Path -LiteralPath")

if (Get-Command git -ErrorAction SilentlyContinue) {
    Push-Location $workspace
    try {
        foreach ($activePath in @(
            "config/system.env",
            "config/api_keys.env",
            "config/providers.json",
            "config/calibration/devices/example/imu-accelerometer.json",
            "providers/rebot_arm_dm/config/arm_calibration.json",
            "providers/rebot_arm_integrated/config/controller.json",
            "skills/stationary_world_arm_alignment/config/alignment.json"
        )) {
            & git check-ignore -q --no-index -- $activePath
            Assert-True -Condition ($LASTEXITCODE -eq 0) -Message "Machine-local path is not ignored: $activePath"
        }
        foreach ($examplePath in @(
            "config/system.env.example",
            "config/api_keys.env.example",
            "config/providers.json.example",
            "config/BASELINE_INVENTORY.md"
        )) {
            & git check-ignore -q --no-index -- $examplePath
            Assert-True -Condition ($LASTEXITCODE -ne 0) -Message "Clean configuration source is unexpectedly ignored: $examplePath"
        }
    }
    finally {
        Pop-Location
    }
}

$temporaryConfig = Join-Path ([System.IO.Path]::GetTempPath()) ("midbrain_config_audit_" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $temporaryConfig | Out-Null
try {
    & (Join-Path $workspace "platform_core/scripts/initialize_config.ps1") -ConfigDirectory $temporaryConfig | Out-Null
    $generatedToExample = @{
        "system.env" = "config/system.env.example"
        "api_keys.env" = "config/api_keys.env.example"
        "providers.json" = "config/providers.json.example"
    }
    foreach ($generatedName in $generatedToExample.Keys) {
        $generatedPath = Join-Path $temporaryConfig $generatedName
        $examplePath = Join-Path $workspace $generatedToExample[$generatedName]
        Assert-True -Condition (Test-Path -LiteralPath $generatedPath -PathType Leaf) -Message "Initializer did not create $generatedName"
        if (Test-Path -LiteralPath $generatedPath -PathType Leaf) {
            Assert-True `
                -Condition ((Get-NormalizedText $generatedPath) -ceq (Get-NormalizedText $examplePath)) `
                -Message "Initializer output $generatedName differs from its clean example"
        }
    }

    $preserved = @{
        "system.env" = "MANAGER_URL=http://example.invalid`n"
        "api_keys.env" = "OPENAI_API_KEY=local-placeholder`n"
        "providers.json" = "{`"providers`":[]}`n"
    }
    foreach ($name in $preserved.Keys) {
        [System.IO.File]::WriteAllText(
            (Join-Path $temporaryConfig $name),
            $preserved[$name],
            [System.Text.UTF8Encoding]::new($false)
        )
    }
    & (Join-Path $workspace "platform_core/scripts/initialize_config.ps1") -ConfigDirectory $temporaryConfig | Out-Null
    foreach ($name in $preserved.Keys) {
        Assert-True `
            -Condition ((Get-NormalizedText (Join-Path $temporaryConfig $name)) -ceq $preserved[$name]) `
            -Message "Initializer overwrote existing local file $name"
    }
}
finally {
    if (Test-Path -LiteralPath $temporaryConfig) {
        Remove-Item -LiteralPath $temporaryConfig -Recurse -Force
    }
}

if ($failures.Count -gt 0) {
    throw "Configuration baseline audit failed:`n$($failures -join "`n")"
}

Write-Host "Configuration baseline audit passed."
Write-Host "Verified $($requiredFiles.Count) required clean sources, deterministic initialization, preservation, ignore rules, and runtime references."
