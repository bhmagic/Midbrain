param(
    [Alias("Python")]
    [string]$PythonLauncher = "",
    [string]$CudaArchitectures = "120",
    [switch]$SkipModels,
    [switch]$SkipEngineBuild
)

$ErrorActionPreference = "Stop"
$providerRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$projectRoot = (Resolve-Path (Join-Path $providerRoot "..\..")).Path
$venv = Join-Path $providerRoot ".venv"
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
    $pythonCommands = @(
        Get-Command python -CommandType Application -All -ErrorAction SilentlyContinue
    )
    foreach ($pythonCommand in $pythonCommands) {
        & $pythonCommand.Source -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)"
        if ($LASTEXITCODE -eq 0) {
            $PythonLauncher = $pythonCommand.Source
            break
        }
    }
    if (-not $PythonLauncher) {
        $pyCommands = @(
            Get-Command py -CommandType Application -All -ErrorAction SilentlyContinue
        )
        foreach ($pyCommand in $pyCommands) {
            $previousPreference = $ErrorActionPreference
            $ErrorActionPreference = "SilentlyContinue"
            $candidates = @(
                & $pyCommand.Source -3.11 -c "import sys; print(sys.executable)" 2>$null
            )
            $launcherExitCode = $LASTEXITCODE
            $ErrorActionPreference = $previousPreference
            if ($launcherExitCode -eq 0 -and $candidates.Count -gt 0) {
                $resolved = [string]$candidates[-1]
                if (Test-Path -LiteralPath $resolved -PathType Leaf) {
                    $PythonLauncher = $resolved
                    break
                }
            }
        }
    }
}
if (-not $PythonLauncher -or -not (Test-Path -LiteralPath $PythonLauncher -PathType Leaf)) {
    throw "Python 3.11 was not found. Pass -PythonLauncher with a working executable."
}
& $PythonLauncher -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)"
if ($LASTEXITCODE -ne 0) { throw "FoundationPose requires Python 3.11." }
if (-not (Test-Path -LiteralPath $venvPython)) {
    & $PythonLauncher -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "FoundationPose environment creation failed" }
}
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -e "$projectRoot\contracts\python"
if ($LASTEXITCODE -ne 0) { throw "BufferRef client installation failed" }
& $venvPython -m pip install -e "$providerRoot\python[runtime,test]"
if ($LASTEXITCODE -ne 0) { throw "FoundationPose package installation failed" }

$vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
$vsInstallations = @(
    if (Test-Path -LiteralPath $vswhere) {
        & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
    }
)
if ($vsInstallations.Count -eq 0) { throw "Visual Studio 2022 C++ Build Tools are required" }
$vsInstallation = [string]($vsInstallations | Select-Object -Last 1)
$vsDevCmd = Join-Path $vsInstallation "Common7\Tools\VsDevCmd.bat"
$bundledCmake = Join-Path $vsInstallation "Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
$cmakeCommand = Get-Command cmake -CommandType Application -ErrorAction SilentlyContinue
$cmake = if ($null -ne $cmakeCommand) { $cmakeCommand.Source } else { $bundledCmake }
if (-not (Test-Path -LiteralPath $vsDevCmd) -or -not (Test-Path -LiteralPath $cmake)) {
    throw "Visual Studio 2022 Build Tools with CMake are required"
}
$nativeRoot = Join-Path $providerRoot "native"
$nativeBuild = Join-Path $nativeRoot "build-win"
# Normalize the duplicated Path/PATH variables inherited by some desktop hosts before MSBuild starts.
$configure = "set PATH=& call `"$vsDevCmd`" -arch=x64 && `"$cmake`" -S `"$nativeRoot`" -B `"$nativeBuild`" -G `"Visual Studio 17 2022`" -A x64 -DCMAKE_CUDA_ARCHITECTURES=$CudaArchitectures"
& cmd.exe /d /c $configure
if ($LASTEXITCODE -ne 0) { throw "FoundationPose native configure failed" }
$build = "set PATH=& call `"$vsDevCmd`" -arch=x64 && `"$cmake`" --build `"$nativeBuild`" --config Release --parallel 8"
& cmd.exe /d /c $build
if ($LASTEXITCODE -ne 0) { throw "FoundationPose native build failed" }

$modelDir = Join-Path $providerRoot "runtime\models"
$engineDir = Join-Path $providerRoot "runtime\engines"
if (-not $SkipModels) {
    New-Item -ItemType Directory -Force -Path $modelDir | Out-Null
    $downloads = @{
        "refine_model.onnx" = "https://api.ngc.nvidia.com/v2/models/nvidia/isaac/foundationpose/versions/1.0.1_onnx/files/refine_model.onnx"
        "score_model.onnx" = "https://api.ngc.nvidia.com/v2/models/nvidia/isaac/foundationpose/versions/1.0.1_onnx/files/score_model.onnx"
    }
    foreach ($name in $downloads.Keys) {
        $target = Join-Path $modelDir $name
        $requiresDownload = -not (Test-Path -LiteralPath $target)
        if (-not $requiresDownload) {
            $requiresDownload = (Get-Item -LiteralPath $target).Length -lt 1048576
        }
        if ($requiresDownload) {
            & curl.exe -L --fail --output $target $downloads[$name]
            if ($LASTEXITCODE -ne 0) { throw "NVIDIA NGC model download failed: $name" }
        }
        if ((Get-Item -LiteralPath $target).Length -lt 1048576) {
            throw "NVIDIA NGC model download is unexpectedly small: $name"
        }
    }
}
if (-not $SkipEngineBuild) {
    if (-not (Test-Path -LiteralPath (Join-Path $modelDir "refine_model.onnx")) -or -not (Test-Path -LiteralPath (Join-Path $modelDir "score_model.onnx"))) {
        throw "Official NVIDIA ONNX models are required before TensorRT engine generation"
    }
    & $venvPython (Join-Path $PSScriptRoot "build_engines.py") --model-dir $modelDir --engine-dir $engineDir
    if ($LASTEXITCODE -ne 0) { throw "FoundationPose TensorRT engine build failed" }
}
& $venvPython -m pytest "$providerRoot\python\tests" -q
if ($LASTEXITCODE -ne 0) { throw "FoundationPose Provider tests failed" }
Write-Output "FoundationPose Provider setup completed: $providerRoot"
