@echo off
setlocal
rem Local ZIP launcher: bypass applies only to this reviewed installer process.
rem No execution policy is changed persistently.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set "code=%ERRORLEVEL%"
endlocal & exit /b %code%
