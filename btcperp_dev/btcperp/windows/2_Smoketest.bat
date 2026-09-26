@echo off
setlocal
cd /d "%~dp0.."
title btcperp - smoketest
if not exist "venv\Scripts\python.exe" goto notinstalled
echo SMOKETEST - run once before going live, and after every new proxy key.
echo.
echo   YES = full live test: REAL orders at minimum size. Opens and closes a tiny
echo         BTC position (costs a little in fees), places and cancels test orders.
echo   W   = like YES, plus the withdrawal probe: asks the exchange to withdraw 1 base unit with
echo         the PROXY key to your own wallet. It must be REJECTED. Required once before going live.
echo   R   = read-only checks only (prices, region, key, account). No orders.
echo.
set "ANS="
set /p "ANS=Type YES, W or R and press Enter: "
if /i "%ANS%"=="YES" goto full
if /i "%ANS%"=="W" goto probe
if /i "%ANS%"=="R" goto readonly
echo Cancelled.
pause
exit /b 0
:full
"venv\Scripts\python.exe" run.py smoketest
goto done
:probe
"venv\Scripts\python.exe" run.py smoketest --probe-withdrawal
goto done
:readonly
"venv\Scripts\python.exe" run.py smoketest --no-trade
:done
echo.
echo Result file: data\smoketest\   (screenshot the SUMMARY above - never the .env file)
pause
exit /b 0
:notinstalled
echo btcperp is not installed yet. Double-click windows\1_Install.bat first.
pause
exit /b 1
