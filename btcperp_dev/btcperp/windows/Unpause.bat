@echo off
setlocal
cd /d "%~dp0.."
title btcperp - unpause
if not exist "venv\Scripts\python.exe" goto notinstalled
echo UNPAUSE: removes only YOUR manual pause (Pause_New_Entries.bat).
echo Kill switches and the equity floor stay active; the drawdown peak and the losing-streak
echo count are not changed. Use this after an upgrade.
echo.
"venv\Scripts\python.exe" run.py unpause
echo.
pause
exit /b 0
:notinstalled
echo btcperp is not installed yet. Double-click windows\1_Install.bat first.
pause
exit /b 1
