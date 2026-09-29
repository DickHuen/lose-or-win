@echo off
setlocal
cd /d "%~dp0.."
title btcperp - preview
if not exist "venv\Scripts\python.exe" goto notinstalled
echo PREVIEW: what the bot would decide right now, with the reasons.
echo Read-only: public market data only, no orders, no keys needed.
echo.
"venv\Scripts\python.exe" run.py preview
echo.
pause
exit /b 0
:notinstalled
echo btcperp is not installed yet. Double-click windows\1_Install.bat first.
pause
exit /b 1
