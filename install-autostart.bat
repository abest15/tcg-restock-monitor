@echo off
REM Starts the monitor (minimized) every time you log in to Windows.
cd /d "%~dp0"
schtasks /Create /F /SC ONLOGON /TN "TCG Restock Monitor" /TR "cmd /c start \"TCG Restock Monitor\" /min \"%~dp0start.bat\""
echo Done. The monitor will start automatically at login.
echo Tip: in Windows Settings ^> System ^> Power, set sleep to "Never" while plugged in. A sleeping PC can't watch drops.
pause
