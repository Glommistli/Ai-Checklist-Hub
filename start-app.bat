@echo off
setlocal
cd /d "%~dp0"
set "APP_EXE=%CD%\desktop_dist\2.2.0\AI Checklist Hub\AI Checklist Hub.exe"
if not exist "%APP_EXE%" (
  echo Desktop application is missing. Running desktop build...
  call build-desktop.bat
  if errorlevel 1 (
    pause
    exit /b 1
  )
)
start "" "%APP_EXE%"
