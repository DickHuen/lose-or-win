@echo off
setlocal
cd /d "%~dp0.."
title btcperp - live vs replay check (v2.0.0)
if not exist "venv\Scripts\python.exe" goto notinstalled
echo Recomputes the bot's live 15-minute decisions of the last 3 days from the stored candles.
echo "DIFFERENT: 0" means live and backtest code decide the same on the same data. Read-only.
echo.
"venv\Scripts\python.exe" run.py intraday-replay --days 3
echo.
pause
exit /b 0
:notinstalled
echo Not installed yet: run windows\1_Install.bat first.
pause
exit /b 1
