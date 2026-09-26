@echo off
setlocal
cd /d "%~dp0.."
title btcperp - KILL
if not exist "venv\Scripts\python.exe" goto notinstalled
echo KILL: closes the open position NOW with a reduce-only market order,
echo cancels its SL/TP orders by id, and pauses the bot until you resume.
echo.
set "ANS="
set /p "ANS=Type KILL and press Enter to close the position: "
if /i not "%ANS%"=="KILL" (
  echo Cancelled. Nothing was changed.
  pause
  exit /b 0
)
"venv\Scripts\python.exe" run.py kill
echo.
pause
exit /b 0
:notinstalled
echo btcperp is not installed yet. Double-click windows\1_Install.bat first.
pause
exit /b 1
