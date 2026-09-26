@echo off
setlocal
cd /d "%~dp0.."
title btcperp dashboard
if not exist "venv\Scripts\python.exe" goto notinstalled
"venv\Scripts\python.exe" run.py dashboard
if errorlevel 1 pause
exit /b 0
:notinstalled
echo btcperp is not installed yet. Double-click windows\1_Install.bat first.
pause
exit /b 1
