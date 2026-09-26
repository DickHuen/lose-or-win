@echo off
setlocal
cd /d "%~dp0.."
title btcperp - install / upgrade
echo btcperp install / upgrade  (folder: %CD%)
echo Safe to run again. Never touches data\, logs\ or .env
echo.
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo Python was not found.
  echo Install Python 3.12 from https://www.python.org/downloads/windows/
  echo and tick "Add python.exe to PATH" on the first screen of the installer.
  pause
  exit /b 1
)
%PY% install.py
echo.
pause
