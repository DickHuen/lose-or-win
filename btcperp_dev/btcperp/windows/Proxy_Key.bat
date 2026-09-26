@echo off
setlocal
cd /d "%~dp0.."
title btcperp - proxy key
if not exist "venv\Scripts\python.exe" goto notinstalled
echo PROXY KEY: the bot trades with a proxy key. It is created HERE; your MAIN wallet only
echo signs a message authorising it. Your main wallet private key never comes to this computer.
echo.
echo   N = new key, sign now in this computer's browser wallet (MetaMask; hardware wallet best)
echo   O = new key, sign on ANOTHER computer (writes the files to copy there)
echo   F = finish: paste a signature made on another computer
echo   S = status
echo.
set "ANS="
set /p "ANS=Type N, O, F or S and press Enter: "
if /i "%ANS%"=="N" goto new
if /i "%ANS%"=="O" goto offline
if /i "%ANS%"=="F" goto finish
if /i "%ANS%"=="S" goto status
echo Cancelled.
pause
exit /b 0
:new
"venv\Scripts\python.exe" run.py proxykey new
goto done
:offline
"venv\Scripts\python.exe" run.py proxykey new --offline
goto done
:finish
"venv\Scripts\python.exe" run.py proxykey finish
goto done
:status
"venv\Scripts\python.exe" run.py proxykey status
:done
echo.
pause
exit /b 0
:notinstalled
echo btcperp is not installed yet. Double-click windows\1_Install.bat first.
pause
exit /b 1
