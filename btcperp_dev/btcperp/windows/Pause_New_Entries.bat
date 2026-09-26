@echo off
setlocal
cd /d "%~dp0.."
title btcperp - pause
if not exist "venv\Scripts\python.exe" goto notinstalled
echo PAUSE: stops NEW entries. An open position and its SL/TP stay as they are.
"venv\Scripts\python.exe" run.py pause
echo.
pause
exit /b 0
:notinstalled
echo btcperp is not installed yet. Double-click windows\1_Install.bat first.
pause
exit /b 1
