param(
    [ValidateSet("auto", "foundation_base_vlm_gripper", "foundation_base_gripper", "vlm_gripper_only")]
    [string]$Mode = "auto",
    [switch]$ArmIsHome,
    [switch]$AllowActiveControlInterrupt
)

$ErrorActionPreference = "Stop"
$SkillRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$VenvPython = Join-Path $SkillRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $VenvPython)) {
    throw "Skill environment is missing. Run scripts\setup.ps1 first."
}

$Arguments = @("-m", "stationary_world_arm_alignment.cli", "--mode", $Mode)
if ($ArmIsHome) {
    $Arguments += "--arm-is-home"
}
if ($AllowActiveControlInterrupt) {
    $Arguments += "--allow-active-control-interrupt"
}
& $VenvPython @Arguments
exit $LASTEXITCODE
