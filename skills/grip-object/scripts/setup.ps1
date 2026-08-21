param([string]$PythonLauncher = "python")
$ErrorActionPreference = "Stop"
$skillRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runtimeRoot = (Resolve-Path (Join-Path $skillRoot "..\grip_work_runtime")).Path
$venv = Join-Path $skillRoot ".venv"
$python = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $python)) {
    if ($PythonLauncher -eq "py") { & py -3.11 -m venv $venv } else { & $PythonLauncher -m venv $venv }
    if ($LASTEXITCODE -ne 0) { throw "Could not create Grip Object Skill environment." }
}
& $python -m pip install --upgrade pip setuptools wheel
if ($LASTEXITCODE -ne 0) { throw "Could not update packaging tools." }
& $python -m pip install "pytest>=8,<10" -e $runtimeRoot -e $skillRoot
if ($LASTEXITCODE -ne 0) { throw "Grip Object Skill installation failed." }
$config = Join-Path $skillRoot "config"
New-Item -ItemType Directory -Force -Path $config | Out-Null
$profiles = Join-Path $config "motion_profiles.json"
if (-not (Test-Path $profiles)) {
    Copy-Item (Join-Path $skillRoot "config_templates\motion_profiles.default.json") $profiles
}
else {
    $profileDocument = Get-Content -Raw -LiteralPath $profiles | ConvertFrom-Json
    $profileChanged = $false
    foreach ($profile in $profileDocument.profiles) {
        foreach ($delayField in @(
            "delay_after_lower_s",
            "delay_after_scrap_s",
            "delay_after_grip_s"
        )) {
            if ($null -eq $profile.PSObject.Properties[$delayField]) {
                $profile | Add-Member -NotePropertyName $delayField -NotePropertyValue 1.5
                $profileChanged = $true
            }
        }
        if (
            $profile.name -eq "development-conservative" -and
            (
                [math]::Abs([double]$profile.grip_position_rad - -4.5) -lt 0.000000001 -or
                [math]::Abs([double]$profile.grip_position_rad - -0.3490658503988659) -lt 0.000000001 -or
                [math]::Abs([double]$profile.grip_position_rad - -0.17453292519943295) -lt 0.000000001 -or
                [math]::Abs([double]$profile.grip_position_rad) -lt 0.000000001
            )
        ) {
            $profile.grip_position_rad = 0.20943951023931953
            $profileChanged = $true
        }
        if (
            $profile.name -eq "development-conservative" -and
            [math]::Abs([double]$profile.grip_velocity_rad_s - 0.35) -lt 0.000000001 -and
            [math]::Abs([double]$profile.contact_timeout_s - 3.0) -lt 0.000000001
        ) {
            $profile.grip_velocity_rad_s = 0.7
            $profile.contact_timeout_s = 10.0
            $profileChanged = $true
        }
        if (
            $profile.name -eq "development-conservative" -and
            [math]::Abs([double]$profile.grip_velocity_rad_s - 0.7) -lt 0.000000001 -and
            [math]::Abs([double]$profile.contact_timeout_s - 10.0) -lt 0.000000001
        ) {
            $profile.grip_velocity_rad_s = 4.0
            $profileChanged = $true
        }
    }
    if ($profileChanged) {
        $payload = ($profileDocument | ConvertTo-Json -Depth 20) + "`n"
        [System.IO.File]::WriteAllText(
            $profiles,
            $payload,
            [System.Text.UTF8Encoding]::new($false)
        )
        Write-Host "Migrated scrap-grip profiles to the current close target, speed, contact timeout, and per-stage waits."
    }
}
$vectorProfiles = Join-Path $config "gripper_vector_profiles.json"
if (-not (Test-Path $vectorProfiles)) {
    Copy-Item (Join-Path $skillRoot "config_templates\gripper_vector_profiles.default.json") $vectorProfiles
}
Write-Host "Grip Object Skill environment ready: $venv"
Write-Host "Local grip motion profiles: $profiles"
Write-Host "Local scrap-grip gripper vector profiles: $vectorProfiles"
