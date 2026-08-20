param([string]$PythonLauncher = "")

. (Join-Path $PSScriptRoot "common.ps1")
$agent = Get-AgentRoot
$workspace = Get-WorkspaceRoot
$configDir = Join-Path $workspace "config"
New-Item -ItemType Directory -Force -Path $configDir | Out-Null

function Resolve-DefaultPythonLauncher {
    $pythonCommands = @(
        Get-Command python -CommandType Application -All -ErrorAction SilentlyContinue
    )
    foreach ($pythonCommand in $pythonCommands) {
        & $pythonCommand.Source -c (
            "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
        )
        if ($LASTEXITCODE -eq 0) {
            return $pythonCommand.Source
        }
    }

    $pyCommands = @(
        Get-Command py -CommandType Application -All -ErrorAction SilentlyContinue
    )
    foreach ($pyCommand in $pyCommands) {
        $previousPreference = $ErrorActionPreference
        $ErrorActionPreference = "SilentlyContinue"
        $resolvedPaths = @(
            & $pyCommand.Source -3.11 -c "import sys; print(sys.executable)" 2>$null
        )
        $launcherExitCode = $LASTEXITCODE
        $ErrorActionPreference = $previousPreference
        if ($launcherExitCode -eq 0 -and $resolvedPaths.Count -gt 0) {
            $resolvedPath = [string]$resolvedPaths[-1]
            if (Test-Path -LiteralPath $resolvedPath -PathType Leaf) {
                return $resolvedPath
            }
        }
    }

    throw "Python 3.11 or newer is required. Pass -PythonLauncher with a working executable."
}

if ([string]::IsNullOrWhiteSpace($PythonLauncher)) {
    $PythonLauncher = Resolve-DefaultPythonLauncher
}

$venv = Join-Path $agent ".venv"
$python = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $python)) {
    if ($PythonLauncher -eq "py") {
        & py -3.11 -m venv $venv
    }
    else {
        & $PythonLauncher -m venv $venv
    }
    if ($LASTEXITCODE -ne 0) { throw "Test-agent virtual environment creation failed." }
}
& $python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "Test-agent pip upgrade failed." }

$bufferRefClient = Join-Path $workspace "contracts\python"
& $python -m pip install -e $bufferRefClient
if ($LASTEXITCODE -ne 0) { throw "BufferRef client installation failed." }

$providerPython = Join-Path $workspace "providers\orbbec_femto_bolt\python"
if (Test-Path (Join-Path $providerPython "pyproject.toml")) {
    & $python -m pip install -e $providerPython
    if ($LASTEXITCODE -ne 0) { throw "Provider support package installation failed." }
}
else {
    Write-Host "Orbbec provider package is not present; RGB/depth capture will be unavailable." -ForegroundColor Yellow
}

$spatialSkill = Join-Path $workspace "skills\spatial_registration_rgbd"
if (Test-Path (Join-Path $spatialSkill "pyproject.toml")) {
    & $python -m pip install -e $spatialSkill
    if ($LASTEXITCODE -ne 0) { throw "Spatial RGB-D Skill installation failed." }
}

$locateArmBaseSkill = Join-Path $workspace "skills\locate_arm_base"
if (Test-Path (Join-Path $locateArmBaseSkill "pyproject.toml")) {
    & $python -m pip install -e $locateArmBaseSkill
    if ($LASTEXITCODE -ne 0) { throw "Locate Arm Base Skill installation failed." }
}

$toolRegistrationSkill = Join-Path $workspace "skills\register_tool_to_control_frame"
if (Test-Path (Join-Path $toolRegistrationSkill "pyproject.toml")) {
    & $python -m pip install -e $toolRegistrationSkill
    if ($LASTEXITCODE -ne 0) { throw "Tool registration Skill installation failed." }
}

$effectorFrontSkill = Join-Path $workspace "skills\locate-effector-front"
if (Test-Path (Join-Path $effectorFrontSkill "pyproject.toml")) {
    & $python -m pip install -e $effectorFrontSkill
    if ($LASTEXITCODE -ne 0) { throw "Effector-front Skill installation failed." }
}

$itemLocatorSkill = Join-Path $workspace "skills\observe_pointed_object"
if (Test-Path (Join-Path $itemLocatorSkill "pyproject.toml")) {
    & $python -m pip install -e $itemLocatorSkill
    if ($LASTEXITCODE -ne 0) { throw "Item-locator Skill installation failed." }
}

$limitedGraphSkill = Join-Path $workspace "skills\limited-graph"
if (Test-Path (Join-Path $limitedGraphSkill "pyproject.toml")) {
    & $python -m pip install -e $limitedGraphSkill
    if ($LASTEXITCODE -ne 0) { throw "Limited Graph Skill installation failed." }
}

$contactRuntime = Join-Path $workspace "skills\contact_work_runtime"
$slicingSkill = Join-Path $workspace "skills\slicing"
if (
    (Test-Path (Join-Path $contactRuntime "pyproject.toml")) -and
    (Test-Path (Join-Path $slicingSkill "pyproject.toml"))
) {
    & $python -m pip install -e $contactRuntime -e $slicingSkill
    if ($LASTEXITCODE -ne 0) { throw "Slicing Skill host support installation failed." }
}

$skillsRoot = Join-Path $workspace "skills"
foreach ($skillDirectory in Get-ChildItem -LiteralPath $skillsRoot -Directory) {
    $manifestPath = Join-Path $skillDirectory.FullName "manifest.json"
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        continue
    }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    $installationProperty = $manifest.PSObject.Properties["installation"]
    if ($null -eq $installationProperty) {
        continue
    }
    $setupProperty = (
        $installationProperty.Value.PSObject.Properties["setup_entrypoint"]
    )
    if ($null -eq $setupProperty) {
        continue
    }
    $setupEntry = [string]$setupProperty.Value
    if (-not $setupEntry) {
        continue
    }
    $setupPath = [System.IO.Path]::GetFullPath(
        (Join-Path $skillDirectory.FullName $setupEntry)
    )
    $skillPrefix = $skillDirectory.FullName.TrimEnd("\", "/") + (
        [System.IO.Path]::DirectorySeparatorChar
    )
    if (-not $setupPath.StartsWith(
        $skillPrefix,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Skill setup entrypoint escaped its Skill directory: $manifestPath"
    }
    if (-not (Test-Path -LiteralPath $setupPath -PathType Leaf)) {
        throw "Skill setup entrypoint is unavailable: $setupPath"
    }
    & $setupPath -PythonLauncher $PythonLauncher
    if ($LASTEXITCODE -ne 0) {
        throw "Skill-private environment setup failed: $($skillDirectory.Name)"
    }
}

& $python -m pip install -e "$(Join-Path $agent 'python')[test]"
if ($LASTEXITCODE -ne 0) { throw "Test-agent package installation failed." }

$keyFile = Join-Path $configDir "api_keys.env"
if (-not (Test-Path $keyFile)) {
    $keyTemplate = Join-Path $configDir "api_keys.env.example"
    if (-not (Test-Path -LiteralPath $keyTemplate)) {
        $keyTemplate = Join-Path $agent "config_templates\api_keys.env.example"
    }
    Copy-Item -LiteralPath $keyTemplate -Destination $keyFile
    Write-Host "Created $keyFile"
}
else {
    Write-Host "Kept existing $keyFile"
}
$systemFile = Join-Path $configDir "system.env"
if (-not (Test-Path $systemFile)) {
    $systemTemplate = Join-Path $configDir "system.env.example"
    if (-not (Test-Path -LiteralPath $systemTemplate)) {
        $systemTemplate = Join-Path $agent "config_templates\system.env.example"
    }
    Copy-Item -LiteralPath $systemTemplate -Destination $systemFile
}
$systemLines = @(Get-Content -LiteralPath $systemFile)
$eligiblePrefix = "PHASE4_ELIGIBLE_TOOLS="
$eligibleIndex = -1
for ($index = 0; $index -lt $systemLines.Count; $index++) {
    if ($systemLines[$index].StartsWith($eligiblePrefix)) {
        $eligibleIndex = $index
        break
    }
}
$requiredSpatialTools = @(
    "establish_world_axis",
    "locate_arm_base",
    "locate_effector_front",
    "locate_item",
    "plan_no_contact_item_approach",
    "inspect_arm_semantic_scene",
    "derive_fabric_world_point",
    "translate_fabric_direction_to_world",
    "translate_fabric_pose_to_world",
    "refine_arm_root_translation",
    "run_limited_graph"
)
if ($eligibleIndex -ge 0) {
    $eligibleValues = [System.Collections.Generic.List[string]]::new()
    foreach ($value in $systemLines[$eligibleIndex].Substring(
        $eligiblePrefix.Length
    ).Split(",")) {
        $trimmed = $value.Trim()
        if ($trimmed -and -not $eligibleValues.Contains($trimmed)) {
            $eligibleValues.Add($trimmed)
        }
    }
    foreach ($toolName in $requiredSpatialTools) {
        if (-not $eligibleValues.Contains($toolName)) {
            $eligibleValues.Add($toolName)
        }
    }
    $systemLines[$eligibleIndex] = $eligiblePrefix + (
        $eligibleValues -join ","
    )
    [System.IO.File]::WriteAllLines(
        $systemFile,
        $systemLines,
        [System.Text.UTF8Encoding]::new($false)
    )
}
Write-Host "Test-agent/OpenAI Agents SDK environment ready: $venv"
