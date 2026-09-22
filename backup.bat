@echo off
setlocal
cd /d "%~dp0"
if not exist backups mkdir backups
for /f "tokens=1-4 delims=/ " %%a in ('date /t') do set stamp=%%a-%%b-%%c-%%d
for /f "tokens=1-3 delims=:., " %%a in ("%time%") do set clock=%%a%%b%%c
set "DESKTOP_DB=%LOCALAPPDATA%\AI Checklist Hub\checklist.db"
if exist "%DESKTOP_DB%" (
  copy /y "%DESKTOP_DB%" "backups\checklist-%stamp%-%clock%.db"
) else if exist backend\data\checklist.db (
  copy /y backend\data\checklist.db "backups\checklist-%stamp%-%clock%.db"
) else (
  echo No database was found.
  exit /b 1
)
echo Backup saved in backups.
