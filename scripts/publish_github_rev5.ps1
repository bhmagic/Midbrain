[CmdletBinding()]
param(
    [string]$RepositoryUrl = "https://github.com/bhmagic/Midbrain.git",
    [string]$CommitMessage = "Publish RGB-D physical AI platform baseline",
    [string]$Branch = "main",
    [switch]$SkipValidation,
    [switch]$SkipRustValidation,
    [switch]$AllowDirtyRemote,
    [switch]$LoginWithGitHubCli
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$workspace = Split-Path $PSScriptRoot -Parent
Set-Location $workspace

function Invoke-ExternalChecked {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $false)][string[]]$Arguments = @()
    )

    $previousErrorActionPreference = $ErrorActionPreference
    try {
        # Windows PowerShell can convert native stderr into a terminating error when
        # ErrorActionPreference is Stop. Let the process finish and inspect its exit code.
        $ErrorActionPreference = "Continue"
        & $FilePath @Arguments
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }

    if ($exitCode -ne 0) {
        throw "Command failed with exit code ${exitCode}: $FilePath $($Arguments -join ' ')"
    }
}

function Invoke-ExternalCapture {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $false)][string[]]$Arguments = @(),
        [switch]$AllowFailure
    )

    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $output = @(& $FilePath @Arguments 2>&1 | ForEach-Object { $_.ToString() })
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }

    if ($exitCode -ne 0 -and -not $AllowFailure) {
        $details = if ($output.Count -gt 0) { "`n$($output -join "`n")" } else { "" }
        throw "Command failed with exit code ${exitCode}: $FilePath $($Arguments -join ' ')$details"
    }

    [pscustomobject]@{
        ExitCode = $exitCode
        Output = $output
    }
}

function Invoke-Git {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    Invoke-ExternalChecked -FilePath "git" -Arguments $Arguments
}

function Invoke-GitCapture {
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [switch]$AllowFailure
    )
    Invoke-ExternalCapture -FilePath "git" -Arguments $Arguments -AllowFailure:$AllowFailure
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "Git is not available on PATH."
}

if (-not $SkipValidation) {
    $validationScript = Join-Path $PSScriptRoot "validate.ps1"
    if ($SkipRustValidation) {
        & $validationScript -SkipRust
    }
    else {
        & $validationScript
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Validation failed; upload was stopped."
    }
}

if (-not (Test-Path (Join-Path $workspace ".git"))) {
    Invoke-Git init
}

# Query the remote list first. A missing origin is normal in a new repository and
# should not be treated as a native-command error by Windows PowerShell.
$remoteNames = (Invoke-GitCapture -Arguments @("remote")).Output
$originExists = @($remoteNames | Where-Object { $_.Trim() -eq "origin" }).Count -gt 0
if (-not $originExists) {
    Invoke-Git remote add origin $RepositoryUrl
}
else {
    $origin = ((Invoke-GitCapture -Arguments @("remote", "get-url", "origin")).Output -join "").Trim()
    if ($origin -ne $RepositoryUrl) {
        Invoke-Git remote set-url origin $RepositoryUrl
    }
}

if ($LoginWithGitHubCli) {
    if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
        throw "GitHub CLI is not available. Install it or omit -LoginWithGitHubCli and use Git Credential Manager."
    }

    $authStatus = Invoke-ExternalCapture -FilePath "gh" -Arguments @("auth", "status", "--hostname", "github.com") -AllowFailure
    if ($authStatus.ExitCode -ne 0) {
        Write-Host "Opening GitHub browser authentication..."
        Invoke-ExternalChecked -FilePath "gh" -Arguments @(
            "auth", "login", "--hostname", "github.com", "--git-protocol", "https", "--web"
        )
    }
    Invoke-ExternalChecked -FilePath "gh" -Arguments @("auth", "setup-git")
}
else {
    Write-Host "GitHub authentication will use existing Git credentials or Git Credential Manager during push."
    Write-Host "To authenticate explicitly with a browser, rerun with -LoginWithGitHubCli."
}

# Refuse accidental non-empty remote overwrite unless explicitly allowed.
$remoteResult = Invoke-GitCapture -Arguments @("ls-remote", "--heads", "origin") -AllowFailure
if ($remoteResult.ExitCode -ne 0) {
    $details = if ($remoteResult.Output.Count -gt 0) { "`n$($remoteResult.Output -join "`n")" } else { "" }
    throw "Unable to read the remote repository. Check the URL, network, and authentication.$details"
}
$remoteHeads = $remoteResult.Output

$headResult = Invoke-GitCapture -Arguments @("rev-parse", "--verify", "HEAD") -AllowFailure
$hasLocalCommit = $headResult.ExitCode -eq 0
if (-not $AllowDirtyRemote -and $remoteHeads.Count -gt 0 -and -not $hasLocalCommit) {
    throw "The remote already contains branches while the local repository has no commit. Clone/fetch and reconcile it, or pass -AllowDirtyRemote after review."
}

Invoke-Git add --all
Invoke-Git diff --cached --check

$stagedFiles = (Invoke-GitCapture -Arguments @("diff", "--cached", "--name-only")).Output
if ($stagedFiles.Count -eq 0) {
    Write-Host "No staged changes. Existing local commit will be pushed if needed."
}
else {
    $blockedPatterns = @(
        '(^|/)api_keys\.env$',
        '(^|/)system\.env$',
        '(^|/)calibration/devices/',
        '(^|/)\.validation/',
        '(^|/)\.venv/',
        '(^|/)target/',
        '(^|/)build/',
        '(^|/)logs?/(?!\.gitkeep$)',
        '(^|/)run/pids?\.json$',
        '\.(exe|dll|lib|pdb|whl|zip|rar|7z)$'
    )
    $blocked = foreach ($file in $stagedFiles) {
        foreach ($pattern in $blockedPatterns) {
            if ($file -match $pattern) {
                $file
                break
            }
        }
    }
    if ($blocked) {
        throw "Blocked machine-local or generated files are staged:`n$($blocked -join "`n")"
    }

    # Use high-confidence credential shapes to avoid matching documentation examples.
    $secretPattern = '(OPENAI_API_KEY=["'']?sk-[A-Za-z0-9_-]{20,}|GEMINI_API_KEY=["'']?AIza[A-Za-z0-9_-]{20,}|-----BEGIN (RSA |OPENSSH |EC )?PRIVATE KEY-----|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})'
    $secretResult = Invoke-GitCapture -Arguments @(
        "grep", "--cached", "-n", "-I", "-E", $secretPattern, "--", "."
    ) -AllowFailure
    if ($secretResult.ExitCode -eq 0 -and $secretResult.Output.Count -gt 0) {
        throw "Potential secrets found in staged content:`n$($secretResult.Output -join "`n")"
    }
    if ($secretResult.ExitCode -gt 1) {
        throw "The staged-content secret scan failed."
    }

    $nameResult = Invoke-GitCapture -Arguments @("config", "--get", "user.name") -AllowFailure
    $emailResult = Invoke-GitCapture -Arguments @("config", "--get", "user.email") -AllowFailure
    $hasIdentity = $nameResult.ExitCode -eq 0 -and $emailResult.ExitCode -eq 0 -and
        -not [string]::IsNullOrWhiteSpace(($nameResult.Output -join "")) -and
        -not [string]::IsNullOrWhiteSpace(($emailResult.Output -join ""))
    if (-not $hasIdentity) {
        throw "Git user.name and user.email must be configured before committing."
    }

    Invoke-Git commit -m $CommitMessage
}

Write-Host "Generated validation output remains local; .validation, target, and native build directories are ignored."
Invoke-Git branch -M $Branch
Invoke-Git push -u origin $Branch
Write-Host "Published $workspace to $RepositoryUrl on branch $Branch."
