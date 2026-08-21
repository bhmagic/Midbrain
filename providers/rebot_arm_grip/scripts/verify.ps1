. (Join-Path $PSScriptRoot "common.ps1")
$providerRoot = Get-ProviderRoot
$python = Get-ProviderPython
& $python -m pip install -e "$providerRoot\python[test]"
if ($LASTEXITCODE -ne 0) { throw "Grip Provider test dependencies failed." }
& $python -m pytest (Join-Path $providerRoot "python\tests") -q
exit $LASTEXITCODE
