param(
    [switch]$NoBrowser,
    [switch]$CoreOnly,
    [switch]$StartAgentUi,
    [switch]$AllowProviderAutoStart,
    [ValidateRange(2, 120)]
    [int]$StartupTimeoutSeconds = 15
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

. (Join-Path $PSScriptRoot "common.ps1")
Repair-DuplicateProcessPath

$workspace = Get-WorkspaceRoot
$core = Get-CoreRoot
$providerConfig = Join-Path $workspace "config\providers.json"
$managerExe = Join-Path $core "target\release\resource-provider-manager.exe"
$fabricExe = Join-Path $core "target\release\world-state-fabric.exe"
$agentPython = Join-Path $workspace "test_agent\.venv\Scripts\python.exe"
$agentLogDirectory = Join-Path $workspace "test_agent\logs"
$agentOutputLog = Join-Path $agentLogDirectory "ui.out.log"
$agentErrorLog = Join-Path $agentLogDirectory "ui.err.log"
$agentLauncherScript = Join-Path $PSScriptRoot "run_agent_ui_logged.ps1"
$pidsFile = Join-Path $core "run\pids.json"

function Test-TcpPortOpen {
    param(
        [Parameter(Mandatory = $true)][int]$Port,
        [int]$TimeoutMilliseconds = 300
    )

    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $connect = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
        if (-not $connect.AsyncWaitHandle.WaitOne($TimeoutMilliseconds)) {
            return $false
        }
        $client.EndConnect($connect)
        return $true
    }
    catch {
        return $false
    }
    finally {
        $client.Dispose()
    }
}

function Start-IndependentProcess {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string]$Arguments = "",
        [hashtable]$Environment = @{}
    )

    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $FilePath
    $startInfo.Arguments = $Arguments
    $startInfo.WorkingDirectory = $workspace
    $startInfo.UseShellExecute = $true
    $startInfo.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden

    $previousEnvironment = @{}
    foreach ($entry in $Environment.GetEnumerator()) {
        $name = [string]$entry.Key
        $previousEnvironment[$name] = [System.Environment]::GetEnvironmentVariable(
            $name,
            [System.EnvironmentVariableTarget]::Process
        )
        [System.Environment]::SetEnvironmentVariable(
            $name,
            [string]$entry.Value,
            [System.EnvironmentVariableTarget]::Process
        )
    }
    try {
        $process = [System.Diagnostics.Process]::Start($startInfo)
    }
    finally {
        foreach ($entry in $previousEnvironment.GetEnumerator()) {
            [System.Environment]::SetEnvironmentVariable(
                [string]$entry.Key,
                $entry.Value,
                [System.EnvironmentVariableTarget]::Process
            )
        }
    }
    if ($null -eq $process) {
        throw "Failed to start $FilePath"
    }
    return $process
}

function Get-RecentProcessFailureLog {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return ""
    }
    return [string](
        Get-Content -LiteralPath $Path -Tail 40 -ErrorAction SilentlyContinue |
            Out-String
    ).Trim()
}

function ConvertTo-QuotedProcessArgument {
    param([Parameter(Mandatory = $true)][string]$Value)

    return '"' + $Value.Replace('"', '\"') + '"'
}

function Wait-BoundedHealth {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [Parameter(Mandatory = $true)][System.Diagnostics.Process]$Process,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        if ($Process.HasExited) {
            throw (
                "Process $($Process.Id) exited with code $($Process.ExitCode) " +
                "before $Url became healthy."
            )
        }
        try {
            $health = Invoke-RestMethod -Uri $Url -TimeoutSec 1
            if ([string]$health.status -eq "ok") {
                return $health
            }
        }
        catch {
            # A connection failure is expected while the process is starting.
        }
        Start-Sleep -Milliseconds 200
    } while ([DateTime]::UtcNow -lt $deadline)

    throw "Timed out after $TimeoutSeconds seconds waiting for $Url"
}

function Assert-RustBinaryCurrent {
    param(
        [Parameter(Mandatory = $true)][string]$BinaryPath,
        [Parameter(Mandatory = $true)][string]$CrateRoot,
        [Parameter(Mandatory = $true)][string]$ComponentName
    )

    $binary = Get-Item -LiteralPath $BinaryPath
    $sourceCandidates = @(
        (Join-Path $core "Cargo.toml"),
        (Join-Path $core "Cargo.lock"),
        (Join-Path $core "rust-toolchain.toml")
    )
    $sourceCandidates += @(
        Get-ChildItem -LiteralPath $CrateRoot -Recurse -File |
            Where-Object {
                $_.Extension -eq ".rs" -or
                $_.Name -eq "Cargo.toml" -or
                $_.Name -eq "build.rs"
            } |
            ForEach-Object { $_.FullName }
    )
    $newestSource = $sourceCandidates |
        Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } |
        ForEach-Object { Get-Item -LiteralPath $_ } |
        Sort-Object LastWriteTimeUtc -Descending |
        Select-Object -First 1
    if (
        $null -ne $newestSource -and
        $newestSource.LastWriteTimeUtc -gt $binary.LastWriteTimeUtc
    ) {
        throw (
            "$ComponentName binary is older than source file " +
            "$($newestSource.FullName). Rebuild Platform Core before " +
            "bounded startup: cargo build --release."
        )
    }
}

if ($CoreOnly -and $StartAgentUi) {
    throw "-CoreOnly and -StartAgentUi cannot be combined."
}

foreach ($required in @(
    $managerExe,
    $fabricExe,
    $providerConfig,
    $agentLauncherScript
)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Missing required file: $required. Run setup_workspace.ps1 first."
    }
}
if ($StartAgentUi -and -not (Test-Path -LiteralPath $agentPython)) {
    throw "Test-agent environment is missing at $agentPython. Run setup_workspace.ps1."
}
Assert-RustBinaryCurrent `
    -BinaryPath $managerExe `
    -CrateRoot (Join-Path $core "manager") `
    -ComponentName "Manager"
Assert-RustBinaryCurrent `
    -BinaryPath $fabricExe `
    -CrateRoot (Join-Path $core "fabric") `
    -ComponentName "Fabric"

$providerDocument = Get-Content -Raw -LiteralPath $providerConfig | ConvertFrom-Json
$unattendedArmAutoStarts = @(
    if ($AllowProviderAutoStart) {
        $providerDocument.providers |
            Where-Object {
                [bool]$_.auto_start -and [string]$_.id -like "robot_arm.*"
            } |
            ForEach-Object { [string]$_.id }
    }
)
if ($unattendedArmAutoStarts.Count -gt 0) {
    throw (
        "Bounded unattended startup refuses auto-start arm providers: " +
        ($unattendedArmAutoStarts -join ", ")
    )
}
$autoStartProviderIds = @(
    if ($AllowProviderAutoStart) {
        $providerDocument.providers |
            Where-Object { [bool]$_.auto_start } |
            ForEach-Object { [string]$_.id }
    }
)

function Stop-BoundedAutoStartProviders {
    param([Nullable[int]]$ManagerPid)

    if ($null -eq $ManagerPid) {
        return
    }
    $manager = Get-Process -Id $ManagerPid -ErrorAction SilentlyContinue
    if ($null -eq $manager) {
        return
    }

    try {
        Invoke-RestMethod -Uri "http://127.0.0.1:7001/health" -TimeoutSec 1 |
            Out-Null
    }
    catch {
        return
    }

    $cleanupDeadline = [DateTime]::UtcNow.AddSeconds(10)
    foreach ($providerId in $autoStartProviderIds) {
        if ([DateTime]::UtcNow -ge $cleanupDeadline) {
            break
        }
        try {
            $escapedProviderId = [Uri]::EscapeDataString($providerId)
            Invoke-RestMethod `
                -Method Post `
                -Uri "http://127.0.0.1:7001/v1/providers/$escapedProviderId/stop" `
                -TimeoutSec 2 |
                Out-Null
        }
        catch {
            # Best-effort cleanup remains bounded; only non-arm providers can be here.
        }
    }
}

$requiredPorts = @(7002, 7001)
if ($StartAgentUi) {
    $requiredPorts += 8000
}
foreach ($port in $requiredPorts) {
    if (Test-TcpPortOpen -Port $port) {
        throw (
            "TCP port $port is already occupied. Refusing to replace or " +
            "stop an existing workspace from the bounded launcher."
        )
    }
}

& (Join-Path $PSScriptRoot "ensure_local_signing_secrets.ps1") | Out-Null
Import-EnvFile (Join-Path $workspace "config\system.env")
Import-EnvFile (Join-Path $workspace "config\api_keys.env")
$env:PHYSICAL_AGENT_ROOT = $workspace

New-Item -ItemType Directory -Force `
    -Path (Join-Path $core "logs"), (Join-Path $core "run"), $agentLogDirectory |
    Out-Null

$started = [System.Collections.Generic.List[System.Diagnostics.Process]]::new()
$fabricProcess = $null
$managerProcess = $null
$uiProcess = $null

try {
    $fabricProcess = Start-IndependentProcess -FilePath $fabricExe
    $started.Add($fabricProcess)
    Wait-BoundedHealth `
        -Url "http://127.0.0.1:7002/health" `
        -Process $fabricProcess `
        -TimeoutSeconds $StartupTimeoutSeconds |
        Out-Null

    $providerAutoStartValue = if ($AllowProviderAutoStart) { "true" } else { "false" }
    $managerArguments = '"' + $providerConfig.Replace('"', '\"') + '"'
    $managerProcess = Start-IndependentProcess `
        -FilePath $managerExe `
        -Arguments $managerArguments `
        -Environment @{
            "MANAGER_PROVIDER_AUTOSTART_ENABLED" = $providerAutoStartValue
        }
    $started.Add($managerProcess)
    $managerHealth = Wait-BoundedHealth `
        -Url "http://127.0.0.1:7001/health" `
        -Process $managerProcess `
        -TimeoutSeconds $StartupTimeoutSeconds
    if (
        -not [bool]$managerHealth.
            workcell_calibration_activation_identity_configured
    ) {
        throw (
            "Manager started without the workcell calibration review secret. " +
            "Check config\api_keys.env and restart the bounded workspace."
        )
    }

    if ($StartAgentUi) {
        Remove-Item `
            -LiteralPath $agentOutputLog, $agentErrorLog `
            -Force `
            -ErrorAction SilentlyContinue
        $agentLauncherArguments =
            "-NoProfile -ExecutionPolicy Bypass -File " +
            (ConvertTo-QuotedProcessArgument $agentLauncherScript) +
            " -PythonPath " +
            (ConvertTo-QuotedProcessArgument $agentPython) +
            " -Workspace " +
            (ConvertTo-QuotedProcessArgument $workspace) +
            " -StandardOutputPath " +
            (ConvertTo-QuotedProcessArgument $agentOutputLog) +
            " -StandardErrorPath " +
            (ConvertTo-QuotedProcessArgument $agentErrorLog)
        $uiProcess = Start-IndependentProcess `
            -FilePath (Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe") `
            -Arguments $agentLauncherArguments `
            -Environment @{
                "AUTO_INITIALIZE_SPACE_COGNITION" = "false"
            }
        $started.Add($uiProcess)
        try {
            Wait-BoundedHealth `
                -Url "http://127.0.0.1:8000/health" `
                -Process $uiProcess `
                -TimeoutSeconds $StartupTimeoutSeconds |
                Out-Null
        }
        catch {
            $failureMessage = $_.Exception.Message
            $errorLog = Get-RecentProcessFailureLog -Path $agentErrorLog
            if ([string]::IsNullOrWhiteSpace($errorLog)) {
                $errorLog = Get-RecentProcessFailureLog -Path $agentOutputLog
            }
            if (-not [string]::IsNullOrWhiteSpace($errorLog)) {
                $failureMessage += "`nAgent UI startup log:`n$errorLog"
            }
            $failureMessage += "`nFull logs: $agentErrorLog and $agentOutputLog"
            throw $failureMessage
        }
        $uiListenerPid = Get-TcpListenerProcessId -Port 8000
        if ($null -eq $uiListenerPid) {
            throw "Agent UI reported no TCP listener on port 8000."
        }
    }

    [ordered]@{
        fabric = $fabricProcess.Id
        manager = $managerProcess.Id
        ui = if ($null -eq $uiProcess) { $null } else { $uiListenerPid }
        ui_launcher = if ($null -eq $uiProcess) { $null } else { $uiProcess.Id }
    } |
        ConvertTo-Json |
        Set-Content -LiteralPath $pidsFile
}
catch {
    $failure = $_
    if ($null -ne $managerProcess) {
        Stop-BoundedAutoStartProviders -ManagerPid $managerProcess.Id
    }
    $startedArray = @($started)
    [array]::Reverse($startedArray)
    foreach ($process in $startedArray) {
        if ($null -ne $process -and -not $process.HasExited) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        }
    }
    Remove-Item -LiteralPath $pidsFile -Force -ErrorAction SilentlyContinue
    throw $failure
}

Write-Host "Manager: http://127.0.0.1:7001"
Write-Host "Fabric:  http://127.0.0.1:7002"
Write-Host "Main UI: http://127.0.0.1:7001/"
if ($null -ne $uiProcess) {
    Write-Host "Developer agent UI: http://127.0.0.1:8000/dev"
}
else {
    Write-Host "Agent UI was not started. Add -StartAgentUi when it is needed."
}
if (-not $AllowProviderAutoStart) {
    Write-Host "Provider auto-start is disabled for this launch."
}
if (-not $NoBrowser) {
    Start-Process "http://127.0.0.1:7001/"
}
Write-Host "PID file: $pidsFile"
Write-Host "Stop: platform_core\scripts\stop_workspace.ps1"
