@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Installation is incomplete. Run install.cmd first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m ops.setup_manager start %*
if errorlevel 1 pause
