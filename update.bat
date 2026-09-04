@echo off
setlocal
cd /d "%~dp0"
title HelixGrid Updater

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0updater\windows-update.ps1"
set "EXITCODE=%errorlevel%"

if not "%EXITCODE%"=="0" (
  echo.
  echo HelixGrid kunne ikke opdateres.
  echo Se beskeden ovenfor for detaljer.
  pause
)

exit /b %EXITCODE%
