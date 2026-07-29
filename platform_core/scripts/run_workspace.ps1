param(
    [switch]$NoBrowser,
    [switch]$CoreOnly,
    [ValidateRange(2, 120)]
    [int]$StartupTimeoutSeconds = 15
)

# Keep the operator-facing entrypoint while using the same bounded lifecycle
# implementation as unattended automation.
& (Join-Path $PSScriptRoot "run_workspace_bounded.ps1") @PSBoundParameters
