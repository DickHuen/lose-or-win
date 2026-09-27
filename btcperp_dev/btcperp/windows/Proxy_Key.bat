@echo off
setlocal
cd /d "%~dp0.."
title btcperp - proxy key
if not exist "venv\Scripts\python.exe" goto notinstalled
echo PROXY KEY: the bot trades with a proxy key. It is created HERE; your MAIN wallet only
echo signs one message authorising it. Your main wallet private key never comes to this computer.
echo The proxy key lasts at most 30 days; an unfinished request is deleted after 1 hour.
echo.
echo   N = new key, sign now in this computer's browser - ONLY with a HARDWARE wallet
echo       (Ledger/Trezor through MetaMask or Rabby). No hardware wallet? Use O.
echo   O = new key, sign on ANOTHER computer (writes one plain file to copy there)
echo   F = finish: paste a signature made on another computer
echo   S = status (pending request, key in .env, all registered proxy keys)
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
echo.
echo Option N signs in THIS computer's browser. A software wallet here would put your main
echo wallet key on the bot computer, which is not allowed. Only continue with a hardware wallet.
set "HW="
set /p "HW=Type HARDWARE to confirm you will sign on a hardware wallet: "
if not "%HW%"=="HARDWARE" (
  echo Not confirmed. Use option O to sign on another computer instead.
  goto done
)
"venv\Scripts\python.exe" run.py proxykey new
goto done
:offline
"venv\Scripts\python.exe" run.py proxykey new --offline
if not errorlevel 1 explorer "data\proxykey"
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
