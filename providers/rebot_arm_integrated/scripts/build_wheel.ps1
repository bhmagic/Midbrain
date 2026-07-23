. (Join-Path $PSScriptRoot "common.ps1")
$root=Get-ProviderRoot;$py=Get-ProviderPython
& $py -m pip install build
& $py -m build --wheel --outdir (Join-Path $root "dist") (Join-Path $root "python")
