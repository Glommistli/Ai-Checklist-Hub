@echo off
setlocal
cd /d "%~dp0"

set "APP_EXE=%CD%\desktop_dist\2.2.0\AI Checklist Hub\AI Checklist Hub.exe"
if not exist "%APP_EXE%" (
  echo Desktop build is missing. Building it now...
  call build-desktop.bat
  if errorlevel 1 goto :error
)

set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
  echo Inno Setup 6 was not found.
  echo Install it with: winget install --id JRSoftware.InnoSetup --exact
  goto :error
)

echo Building Windows installer...
"%ISCC%" "installer\AI Checklist Hub.iss"
if errorlevel 1 goto :error

echo.
echo Installer built successfully:
echo %CD%\installer_dist\AI-Checklist-Hub-Setup-2.2.0.exe
exit /b 0

:error
echo.
echo Installer build failed.
exit /b 1
