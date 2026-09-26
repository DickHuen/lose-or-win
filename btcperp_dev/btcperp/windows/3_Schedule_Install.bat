@echo off
setlocal
cd /d "%~dp0.."
title btcperp - start automatic trading
if not exist "venv\Scripts\python.exe" goto notinstalled
echo This registers the bot's routines in Windows Task Scheduler (folder "btcperp"):
echo   decide 08:30 / 08:50 HKT, manage 5x a day, reports, backup, dashboard at logon.
echo From then on the bot TRADES REAL MONEY automatically while this PC is on.
echo Only do this after the smoketest passed and you decided to go live.
echo.
set "ANS="
set /p "ANS=Type GO and press Enter to start: "
if /i not "%ANS%"=="GO" (
  echo Cancelled. Nothing was changed.
  pause
  exit /b 0
)
"venv\Scripts\python.exe" run.py schedule install
echo.
pause
exit /b 0
:notinstalled
echo btcperp is not installed yet. Double-click windows\1_Install.bat first.
pause
exit /b 1
