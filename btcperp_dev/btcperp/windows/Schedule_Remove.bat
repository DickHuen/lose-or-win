@echo off
setlocal
cd /d "%~dp0.."
title btcperp - stop automatic trading
if not exist "venv\Scripts\python.exe" goto notinstalled
echo This removes all btcperp tasks from Windows Task Scheduler.
echo The bot will stop deciding and managing. An OPEN POSITION STAYS OPEN on the
echo exchange with its SL/TP. Use Kill_Close_Position.bat first if you want it closed.
echo.
set "ANS="
set /p "ANS=Type REMOVE and press Enter: "
if /i not "%ANS%"=="REMOVE" (
  echo Cancelled. Nothing was changed.
  pause
  exit /b 0
)
"venv\Scripts\python.exe" run.py schedule remove
echo.
pause
exit /b 0
:notinstalled
echo btcperp is not installed yet. Double-click windows\1_Install.bat first.
pause
exit /b 1
