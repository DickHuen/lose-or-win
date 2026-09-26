@echo off
setlocal
cd /d "%~dp0.."
title btcperp - resume
if not exist "venv\Scripts\python.exe" goto notinstalled
echo Active pause reasons:
echo.
"venv\Scripts\python.exe" run.py reasons
echo.
echo RESUME clears the pauses above. The equity floor is never cleared here (only a new config
echo version that states the new baseline can). After an upgrade use Unpause.bat instead.
echo Check the dashboard and the reason for each stop before you resume.
echo.
set "ANS="
set /p "ANS=Type RESUME and press Enter: "
if /i not "%ANS%"=="RESUME" (
  echo Cancelled. Nothing was changed.
  pause
  exit /b 0
)
"venv\Scripts\python.exe" run.py resume
if errorlevel 6 goto confirm
echo.
pause
exit /b 0
:confirm
echo.
echo A KILL SWITCH is active (drawdown or losing streak). Resuming it resets the drawdown peak
echo to today's equity and restarts the losing-streak count.
set "ANS2="
set /p "ANS2=Type RESET-PEAK and press Enter to confirm: "
if /i not "%ANS2%"=="RESET-PEAK" (
  echo Cancelled. The kill switch stays active.
  pause
  exit /b 0
)
"venv\Scripts\python.exe" run.py resume --reset-peak
echo.
pause
exit /b 0
:notinstalled
echo btcperp is not installed yet. Double-click windows\1_Install.bat first.
pause
exit /b 1
