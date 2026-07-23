. (Join-Path $PSScriptRoot "common.ps1")
$provider=Get-ProviderRoot;$workspace=Get-WorkspaceRoot;$configFile=Join-Path $workspace "config\providers.json"
New-Item -ItemType Directory -Force -Path (Split-Path $configFile)|Out-Null
if(Test-Path $configFile){$doc=Get-Content $configFile -Raw|ConvertFrom-Json}else{$doc=[pscustomobject]@{providers=@()}}
if($null -eq $doc.providers){$doc|Add-Member NoteProperty providers @() -Force}
$entry=Get-Content (Join-Path $provider "config_templates\provider_entry.json") -Raw|ConvertFrom-Json
$doc.providers=@($doc.providers|Where-Object{$_.id -ne $entry.id})+@($entry)
[IO.File]::WriteAllText($configFile,($doc|ConvertTo-Json -Depth 30)+[Environment]::NewLine,[Text.UTF8Encoding]::new($false))
Write-Host "Registered $($entry.id) in $configFile"
