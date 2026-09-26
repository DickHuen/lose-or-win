@echo off
setlocal
cd /d "%~dp0.."
title btcperp - scheduled tasks
if not exist "venv\Scripts\python.exe" goto notinstalled
"venv\Scripts\python.exe" run.py schedule show
echo.
"venv\Scripts\python.exe" run.py schedule list
echo.
pause
exit /b 0
:notinstalled
echo btcperp is not installed yet. Double-click windows\1_Install.bat first.
pause
exit /b 1
