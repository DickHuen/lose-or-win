@echo off
setlocal
cd /d "%~dp0.."
title btcperp - upgrade
echo UPGRADE btcperp in %CD%
echo Put the new btcperp_vX.Y.Z.zip in your Downloads folder first (do NOT unzip it).
echo The upgrade stops the scheduled bot, waits for a running command to finish, installs the new
echo version, runs the tests and then restarts the scheduled bot. data\, logs\ and .env are kept.
echo Avoid the decision times: HKT 00:30, 04:30, 08:30, 12:30, 16:30, 20:30 (about 30 min either side).
echo.
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo Python was not found. Install Python 3.12 from python.org first.
  pause
  exit /b 1
)
set "ZIP="
for /f "delims=" %%F in ('dir /b /o-d "%USERPROFILE%\Downloads\btcperp_v*.zip" 2^>nul') do (
  if not defined ZIP set "ZIP=%USERPROFILE%\Downloads\%%F"
)
if not defined ZIP (
  echo No btcperp_v*.zip found in %USERPROFILE%\Downloads
  pause
  exit /b 1
)
echo Newest zip found: %ZIP%
echo Its SHA-256 (compare it with the value sent with the release; stop if it differs):
certutil -hashfile "%ZIP%" SHA256 | findstr /v /c:"hash" /c:"CertUtil"
echo Installed version:
type VERSION
echo.
echo An older or equal version is refused. If anything fails, the installed version is restored.
echo.
echo   UPGRADE      = install this zip
echo   TEST-RESTORE = go-live check: install this zip (the same version is allowed), act as if it
echo                  failed, and put the installed version back. Ends with RESTORE TEST PASSED.
set "ANS="
set /p "ANS=Type UPGRADE or TEST-RESTORE and press Enter: "
if /i "%ANS%"=="TEST-RESTORE" (%PY% install.py --from-zip "%ZIP%" --test-restore & echo. & pause & exit /b)
if /i not "%ANS%"=="UPGRADE" (
  echo Cancelled. Nothing was changed.
  pause
  exit /b 0
)
%PY% install.py --from-zip "%ZIP%" & echo. & pause & exit /b
