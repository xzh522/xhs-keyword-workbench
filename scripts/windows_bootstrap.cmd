@echo off
setlocal
cd /d "%~dp0.."
set "PYTHONUTF8=1"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "import sys; assert sys.version_info >= (3,10)" >nul 2>&1
  if not errorlevel 1 (
    ".venv\Scripts\python.exe" "launcher.py" %*
    goto :finish
  )
)
py -3 -c "import sys; assert sys.version_info >= (3,10)" >nul 2>&1
if not errorlevel 1 (
  py -3 "launcher.py" %*
  goto :finish
)
python -c "import sys; assert sys.version_info >= (3,10)" >nul 2>&1
if not errorlevel 1 (
  python "launcher.py" %*
  goto :finish
)
echo Python 3.10 or newer is required. Install Python from https://www.python.org/downloads/windows/
echo Enable the Python launcher and Add python.exe to PATH during installation.
exit /b 1
:finish
exit /b %errorlevel%
