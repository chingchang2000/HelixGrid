@echo off
setlocal
cd /d "%~dp0"
title HelixGrid Windows Installer

net session >nul 2>&1
if not "%errorlevel%"=="0" (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -ArgumentList '%*' -Verb RunAs"
  exit /b
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\windows-install.ps1" %*
set "EXITCODE=%errorlevel%"

if not "%EXITCODE%"=="0" (
  echo.
  echo HelixGrid installationen stoppede med en fejl.
  echo Du kan koere windows-install.bat igen.
  pause
)

exit /b %EXITCODE%
