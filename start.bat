@echo off
setlocal
cd /d "%~dp0"
title HelixGrid

set "DASHBOARD=%~dp0dashboard\helix_dashboard.py"
if not exist "%DASHBOARD%" (
  echo HelixGrid dashboard blev ikke fundet.
  echo Koerer installationen igen...
  call "%~dp0windows-install.bat"
  exit /b
)

where pyw.exe >nul 2>&1
if "%errorlevel%"=="0" (
  start "" pyw.exe -3 "%DASHBOARD%"
  exit /b 0
)

where pythonw.exe >nul 2>&1
if "%errorlevel%"=="0" (
  start "" pythonw.exe "%DASHBOARD%"
  exit /b 0
)

if exist "%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe" (
  start "" "%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe" "%DASHBOARD%"
  exit /b 0
)

echo Python blev ikke fundet. HelixGrid installerer de manglende dele.
call "%~dp0windows-install.bat"
exit /b
