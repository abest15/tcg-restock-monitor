@echo off
REM First-time setup on Windows: installs Python 3.13 if needed, creates a virtual environment,
REM installs packages, runs the setup wizard.
cd /d "%~dp0"
echo %CD% | find /i "OneDrive" >nul && (echo This folder is inside OneDrive. OneDrive locks files and breaks setup. & echo Move the restock-monitor folder to C:\restock-monitor and run setup.bat from there. & pause & exit /b 1)
if exist .venv rmdir /s /q .venv

REM Use Python 3.13: brand-new versions (3.15+) don't have prebuilt packages yet.
echo Making sure Python 3.13 is installed (one time download)...
py install 3.13
py -V:3.13 -c "import sys" >nul 2>nul
if errorlevel 1 (
  echo.
  echo Could not find Python 3.13. Install it from https://www.python.org/downloads/release/python-3139/
  echo ^(Windows installer 64-bit, tick "Add python.exe to PATH"^), then double-click setup.bat again.
  pause
  exit /b 1
)

py -V:3.13 -m venv .venv || (echo Could not create the virtual environment. & pause & exit /b 1)
.venv\Scripts\python -m pip install -q --only-binary=:all: -r requirements.txt || (echo Package install failed - screenshot this window and send it to Claude. & pause & exit /b 1)
.venv\Scripts\python -m restockbot setup
.venv\Scripts\python -m restockbot doctor
echo.
echo Setup finished. Double-click start.bat to start monitoring.
echo To start automatically when you log in, run install-autostart.bat
pause
