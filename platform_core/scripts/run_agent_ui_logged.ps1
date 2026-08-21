param(
    [Parameter(Mandatory = $true)][string]$PythonPath,
    [Parameter(Mandatory = $true)][string]$Workspace,
    [Parameter(Mandatory = $true)][string]$StandardOutputPath,
    [Parameter(Mandatory = $true)][string]$StandardErrorPath
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

Set-Location -LiteralPath $Workspace
try {
    $agentProcess = Start-Process `
        -FilePath $PythonPath `
        -ArgumentList "-m", "physical_agent_test.app" `
        -WorkingDirectory $Workspace `
        -RedirectStandardOutput $StandardOutputPath `
        -RedirectStandardError $StandardErrorPath `
        -WindowStyle Hidden `
        -PassThru `
        -Wait
    $processExitCode = $agentProcess.ExitCode
}
catch {
    $_ | Out-String | Add-Content -LiteralPath $StandardErrorPath
    $processExitCode = 1
}
exit $processExitCode
