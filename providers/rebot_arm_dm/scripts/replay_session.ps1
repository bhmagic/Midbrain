param(
    [Parameter(Mandatory = $true)][string]$Session,
    [switch]$WithoutInertia,
    [switch]$WriteResult
)
. (Join-Path $PSScriptRoot "common.ps1")
$python = Get-PythonPath
if (-not (Test-Path $python)) { throw "Python environment is missing. Run scripts\setup.ps1 first." }
$args = @("-m", "rebot_arm_dm_provider.replay_session", $Session)
if ($WithoutInertia) { $args += "--without-inertia" }
if ($WriteResult) { $args += "--write-result" }
& $python @args
exit $LASTEXITCODE
