@echo off
setlocal
cd /d "%~dp0"
if /I "%~1"=="-Mode" if /I "%~2"=="Docker" (
  set "PYTHON=python.exe"
  where %PYTHON% >nul 2>&1
  if errorlevel 1 (
    echo Docker start needs an existing Python 3 interpreter for the dependency-free bootstrap.
    exit /b 1
  )
  %PYTHON% ops\docker_setup.py start
  exit /b %ERRORLEVEL%
)
if /I "%~1"=="--mode" if /I "%~2"=="docker" (
  python.exe ops\docker_setup.py start
  exit /b %ERRORLEVEL%
)
if not exist ".venv\Scripts\python.exe" (
  echo Installation is incomplete. Run install.cmd first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m ops.setup_manager start %*
if errorlevel 1 pause
