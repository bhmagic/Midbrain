param(
    [Alias("Python")]
    [string]$PythonLauncher = ""
)

$ErrorActionPreference = "Stop"
$skillRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$projectRoot = (Resolve-Path (Join-Path $skillRoot "..\..")).Path
$venv = Join-Path $skillRoot ".venv"
$venvPython = Join-Path $venv "Scripts\python.exe"
if ($PythonLauncher -and -not (Test-Path -LiteralPath $PythonLauncher -PathType Leaf)) {
    $launcherCommand = Get-Command $PythonLauncher -CommandType Application -ErrorAction SilentlyContinue
    if ($null -eq $launcherCommand) {
        throw "Python launcher was not found: $PythonLauncher"
    }
    if ([System.IO.Path]::GetFileNameWithoutExtension($launcherCommand.Source) -eq "py") {
        $previousPreference = $ErrorActionPreference
        $ErrorActionPreference = "SilentlyContinue"
        $candidates = @(
            & $launcherCommand.Source -3.11 -c "import sys; print(sys.executable)" 2>$null
        )
        $launcherExitCode = $LASTEXITCODE
        $ErrorActionPreference = $previousPreference
        $PythonLauncher = if ($launcherExitCode -eq 0 -and $candidates.Count -gt 0) {
            [string]$candidates[-1]
        }
        else {
            ""
        }
    }
    else {
        $PythonLauncher = $launcherCommand.Source
    }
}
if (-not $PythonLauncher) {
    $pythonCommands = @(Get-Command python -CommandType Application -All -ErrorAction SilentlyContinue)
    foreach ($pythonCommand in $pythonCommands) {
        & $pythonCommand.Source -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)"
        if ($LASTEXITCODE -eq 0) {
            $PythonLauncher = $pythonCommand.Source
            break
        }
    }
    if (-not $PythonLauncher) {
        $py = Get-Command py -CommandType Application -ErrorAction SilentlyContinue
        if ($null -ne $py) {
            $previousPreference = $ErrorActionPreference
            $ErrorActionPreference = "SilentlyContinue"
            $candidates = @(& $py.Source -3.11 -c "import sys; print(sys.executable)" 2>$null)
            $launcherExitCode = $LASTEXITCODE
            $ErrorActionPreference = $previousPreference
            if ($launcherExitCode -eq 0 -and $candidates.Count -gt 0) {
                $PythonLauncher = [string]$candidates[-1]
            }
        }
    }
}
if (-not $PythonLauncher -or -not (Test-Path -LiteralPath $PythonLauncher -PathType Leaf)) {
    throw "Python 3.11 was not found. Pass -PythonLauncher with a working executable."
}
& $PythonLauncher -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)"
if ($LASTEXITCODE -ne 0) { throw "Locate Arm Base requires Python 3.11." }
if (-not (Test-Path -LiteralPath $venvPython)) {
    & $PythonLauncher -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "Locate Arm Base environment creation failed" }
}
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -e "$projectRoot\contracts\python"
if ($LASTEXITCODE -ne 0) { throw "BufferRef client installation failed" }
& $venvPython -m pip install -e "$skillRoot[test]"
if ($LASTEXITCODE -ne 0) { throw "Locate Arm Base package installation failed" }
& $venvPython -m pytest "$skillRoot\python\tests" -q
if ($LASTEXITCODE -ne 0) { throw "Locate Arm Base Skill tests failed" }
Write-Output "Locate Arm Base Skill setup completed: $skillRoot"
