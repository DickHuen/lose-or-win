@echo off
setlocal
cd /d "%~dp0.."
title btcperp - alerts
if not exist "venv\Scripts\python.exe" goto notinstalled
echo Unread alerts (they are also on the dashboard). Shown once, then marked read.
echo.
"venv\Scripts\python.exe" run.py alerts
echo.
pause
exit /b 0
:notinstalled
echo btcperp is not installed yet. Double-click windows\1_Install.bat first.
pause
exit /b 1
