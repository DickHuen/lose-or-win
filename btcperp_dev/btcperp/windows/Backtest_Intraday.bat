@echo off
setlocal
cd /d "%~dp0.."
title btcperp - intraday backtest (v2.0.0)
if not exist "venv\Scripts\python.exe" goto notinstalled
echo INTRADAY BACKTEST (v2.0.0): downloads Binance BTCUSDT 5m / 15m / 1h / 4h candles for the last 365 days
echo (public data, a few minutes) and runs the SAME 15-minute rules, sizing, exits and cost gate as the live bot,
echo under 4 cost scenarios. It never trades and never touches your account. Low priority.
echo It is an approximation (Binance prices, 5-minute candles): read the list at the top of the report.
echo.
"venv\Scripts\python.exe" run.py intraday-backtest download --days 365
if errorlevel 1 goto failed
start "" /low /wait /b "venv\Scripts\python.exe" run.py intraday-backtest run --days 60
start "" /low /wait /b "venv\Scripts\python.exe" run.py intraday-backtest run --days 365
echo.
echo Results: data\backtest_intraday\results_...  Send both summary.md files for review.
pause
exit /b 0
:failed
echo Download failed. Check the internet connection and try again.
pause
exit /b 1
:notinstalled
echo Not installed yet: run windows\1_Install.bat first.
pause
exit /b 1
