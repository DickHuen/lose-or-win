@echo off
setlocal
cd /d "%~dp0.."
title btcperp - daily report
if not exist "venv\Scripts\python.exe" goto notinstalled
"venv\Scripts\python.exe" run.py report daily
echo.
echo All reports are saved in data\reports\
pause
exit /b 0
:notinstalled
echo btcperp is not installed yet. Double-click windows\1_Install.bat first.
pause
exit /b 1
