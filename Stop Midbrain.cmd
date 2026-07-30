@echo off
setlocal

pushd "%~dp0"
if errorlevel 1 (
    echo Unable to enter the Midbrain workspace.
    pause
    exit /b 1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0platform_core\scripts\stop_workspace.ps1"
set "MIDBRAIN_EXIT_CODE=%ERRORLEVEL%"
popd

if not "%MIDBRAIN_EXIT_CODE%"=="0" (
    echo.
    echo Midbrain did not stop cleanly. Review the error above.
    pause
)

exit /b %MIDBRAIN_EXIT_CODE%
