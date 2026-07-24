param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$SkillRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$WorkspaceRoot = (Resolve-Path (Join-Path $SkillRoot "..\..")).Path
$VenvPython = Join-Path $SkillRoot ".venv\Scripts\python.exe"

function Invoke-Checked {
    param(
        [string]$Executable,
        [string[]]$Arguments
    )
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Executable exited with code $LASTEXITCODE"
    }
}

if (-not (Test-Path -LiteralPath $VenvPython)) {
    Invoke-Checked $Python @("-m", "venv", (Join-Path $SkillRoot ".venv"))
}

Invoke-Checked $VenvPython @("-m", "pip", "install", "--upgrade", "pip")
Invoke-Checked $VenvPython @("-m", "pip", "install", "-e", $SkillRoot)

$SitePackages = (& $VenvPython -c "import site; print(site.getsitepackages()[0])").Trim()
if ($LASTEXITCODE -ne 0 -or -not $SitePackages) {
    throw "Could not resolve the Skill virtual environment's site-packages folder."
}
$OrbbecPython = (Resolve-Path (Join-Path $WorkspaceRoot "providers\orbbec_femto_bolt\python")).Path
$PathFile = Join-Path $SitePackages "midbrain_orbbec_femto_local.pth"
[System.IO.File]::WriteAllText(
    $PathFile,
    "$OrbbecPython`n",
    [System.Text.UTF8Encoding]::new($false)
)

Write-Host "Skill environment ready: $VenvPython"
