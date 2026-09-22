@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Please run setup.bat first.
  pause
  exit /b 1
)
if not exist "frontend\dist\index.html" (
  echo Production frontend is missing. Run setup.bat first.
  pause
  exit /b 1
)
echo Building native Windows application...
call .venv\Scripts\python.exe -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --windowed ^
  --onedir ^
  --name "AI Checklist Hub" ^
  --icon "assets\ai-checklist-hub.ico" ^
  --paths "backend" ^
  --add-data "backend;backend" ^
  --add-data "frontend\dist;frontend\dist" ^
  --collect-all webview ^
  --collect-submodules fastapi ^
  --collect-submodules starlette ^
  --collect-submodules sqlalchemy ^
  --collect-submodules pydantic ^
  --collect-submodules pydantic_settings ^
  --collect-submodules httpx ^
  --collect-submodules python_multipart ^
  --collect-submodules multipart ^
  --hidden-import uvicorn.logging ^
  --hidden-import uvicorn.loops.auto ^
  --hidden-import uvicorn.protocols.http.auto ^
  --hidden-import uvicorn.protocols.websockets.auto ^
  --distpath "desktop_dist" ^
  --workpath "build\desktop" ^
  "desktop\app.py"
if errorlevel 1 goto :error
echo.
echo Desktop app built successfully:
echo %CD%\desktop_dist\AI Checklist Hub\AI Checklist Hub.exe
exit /b 0
:error
echo Desktop build failed.
exit /b 1
