@echo off
setlocal
cd /d "%~dp0.."
title btcperp - forecast check (v2.3.0)
if not exist "venv\Scripts\python.exe" goto notinstalled
echo FORECAST CHECK (v2.3.0). Read-only: never trades, never touches your account.
echo 1. Every hour of the last 365 days: the forecast the bot would have shown, from the stored Binance 1h candles,
echo    compared with what happened next (ranges, which way first, 500/1000/2000 USD, top / bottom warnings).
echo 2. The intraday rules with and without the forecast / dynamic exit (variants A B C C0 E) on the stored 1h
echo    candles (a 1h PROXY of the 15-minute rules) and, if downloaded, on 15m (windows\Backtest_Intraday.bat).
echo Low priority, a few minutes. Results: data\forecast\  Send the .md files for review (never .env).
echo.
start "" /low /wait /b "venv\Scripts\python.exe" run.py forecast-check --days 365
start "" /low /wait /b "venv\Scripts\python.exe" run.py intraday-compare --days 365 --proxy-1h
if exist "data\backtest_intraday\meta.json" start "" /low /wait /b "venv\Scripts\python.exe" run.py intraday-compare --days 365
echo.
echo Results: data\forecast\
pause
exit /b 0
:notinstalled
echo Not installed yet: run windows\1_Install.bat first.
pause
exit /b 1
