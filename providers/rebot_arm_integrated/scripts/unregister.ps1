. (Join-Path $PSScriptRoot "common.ps1")
$workspace=Get-WorkspaceRoot;$configFile=Join-Path $workspace "config\providers.json";if(-not(Test-Path $configFile)){exit 0}
$doc=Get-Content $configFile -Raw|ConvertFrom-Json;$doc.providers=@($doc.providers|Where-Object{$_.id -ne 'robot_arm.primary.integrated'})
[IO.File]::WriteAllText($configFile,($doc|ConvertTo-Json -Depth 30)+[Environment]::NewLine,[Text.UTF8Encoding]::new($false))
