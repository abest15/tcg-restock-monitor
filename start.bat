@echo off
cd /d "%~dp0"
title TCG Restock Monitor
:loop
.venv\Scripts\python -m restockbot run
if errorlevel 3 if not errorlevel 4 (echo Updated - starting the new version... & goto loop)
echo Monitor stopped. Restarting in 30 seconds (close this window to quit)...
timeout /t 30
goto loop
