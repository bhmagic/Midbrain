param([switch]$Quiet)

$runtimeScript = Join-Path `
    $PSScriptRoot `
    "..\..\grip_work_runtime\scripts\stop_development_server.ps1"
& $runtimeScript -Port 7115 -SkillKind "scrap_grip" -Quiet:$Quiet
exit $LASTEXITCODE
