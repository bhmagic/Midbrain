param([switch]$Quiet)

$runtimeScript = Join-Path `
    $PSScriptRoot `
    "..\..\grip_work_runtime\scripts\stop_development_server.ps1"
& $runtimeScript -Port 7116 -SkillKind "lay_flat" -Quiet:$Quiet
exit $LASTEXITCODE
