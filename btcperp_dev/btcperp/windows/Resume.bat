@echo off
setlocal
cd /d "%~dp0.."
title btcperp - resume
if not exist "venv\Scripts\python.exe" goto notinstalled
echo RESUME: clears pause and kill switches (drawdown, losing streak, manual),
echo resets the drawdown peak to current equity and restarts the losing-streak count.
echo The equity-floor stop is NOT cleared by this (only a new config version can).
echo Check the dashboard and the reason for the stop before you resume.
echo.
set "ANS="
set /p "ANS=Type RESUME and press Enter: "
if /i not "%ANS%"=="RESUME" (
  echo Cancelled. Nothing was changed.
  pause
  exit /b 0
)
"venv\Scripts\python.exe" run.py resume
echo.
pause
exit /b 0
:notinstalled
echo btcperp is not installed yet. Double-click windows\1_Install.bat first.
pause
exit /b 1
