@echo off
setlocal
cd /d "%~dp0.."
title btcperp - backtest
if not exist "venv\Scripts\python.exe" goto notinstalled
echo BACKTEST: downloads Binance BTCUSDT history (public data, a few minutes the first time) and
echo replays the strategy with the live code. It never trades and never touches your account.
echo It runs at low priority so the scheduled bot is not slowed down.
echo.
echo The pass/fail rules below must be confirmed by you BEFORE the first run:
echo.
"venv\Scripts\python.exe" run.py backtest criteria
echo.
"venv\Scripts\python.exe" run.py backtest download
if errorlevel 1 goto failed
start "" /low /wait /b "venv\Scripts\python.exe" run.py backtest run
if errorlevel 6 goto confirm
goto done
:confirm
echo.
echo The rules above are not confirmed yet. Only confirm them after you have read them:
echo they cannot be changed after you see the results.
set "ANS="
set /p "ANS=Type CONFIRM and press Enter: "
if /i not "%ANS%"=="CONFIRM" (
  echo Not confirmed. The backtest did not run.
  pause
  exit /b 0
)
"venv\Scripts\python.exe" run.py backtest confirm
start "" /low /wait /b "venv\Scripts\python.exe" run.py backtest run
:done
echo.
echo Results are in data\backtest\results_...  Send summary.md and summary.json for review.
pause
exit /b 0
:failed
echo Download failed. Check the internet connection and try again.
pause
exit /b 1
:notinstalled
echo btcperp is not installed yet. Double-click windows\1_Install.bat first.
pause
exit /b 1
