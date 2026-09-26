@echo off
setlocal
cd /d "%~dp0.."
if not exist ".env" (
  echo .env does not exist yet. Double-click windows\1_Install.bat first.
  pause
  exit /b 1
)
echo Opening .env in Notepad. Save with Ctrl+S, then close Notepad.
echo Never send this file or a screenshot of it to anyone.
start "" /wait notepad.exe ".env"
