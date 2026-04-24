@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
rem Run from this folder so PowerShell can find chairside_ready_alert.py in the same directory
cd /d "%SCRIPT_DIR%"
powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%install_chairside_ready_alert.ps1"
set "EXIT_CODE=%errorlevel%"

if not "%EXIT_CODE%"=="0" (
  echo.
  echo Installation failed with exit code %EXIT_CODE%.
  pause
)

endlocal
