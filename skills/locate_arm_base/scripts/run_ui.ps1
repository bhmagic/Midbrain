param(
    [int]$Port = 7114,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$skillRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$projectRoot = (Resolve-Path (Join-Path $skillRoot "..\..")).Path
$python = Join-Path $skillRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Locate Arm Base environment is unavailable. Run scripts/setup.ps1 first."
}
function Import-ProcessEnvFile {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return }
    foreach ($line in Get-Content -LiteralPath $Path) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) { continue }
        $parts = $trimmed.Split("=", 2)
        if ($parts.Count -eq 2) {
            [Environment]::SetEnvironmentVariable(
                $parts[0].Trim(),
                $parts[1],
                "Process"
            )
        }
    }
}
Import-ProcessEnvFile (Join-Path $projectRoot "config\system.env")
Import-ProcessEnvFile (Join-Path $projectRoot "config\api_keys.env")
$env:PHYSICAL_AGENT_ROOT = $projectRoot
& (Join-Path $PSScriptRoot "stop_ui.ps1") -Port $Port -Quiet
& $python -m locate_arm_base.app --config (Join-Path $skillRoot "config_templates\skill.default.json") --port $Port
exit $LASTEXITCODE
