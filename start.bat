@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Please run setup.bat first.
  pause
  exit /b 1
)
if not exist "frontend\dist\index.html" (
  echo Production frontend is missing. Please run setup.bat first.
  pause
  exit /b 1
)
powershell.exe -NoProfile -Command "try { $r = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/api/health' -TimeoutSec 1; if ($r.status -eq 'ok') { exit 0 } else { exit 1 } } catch { exit 1 }"
if not errorlevel 1 (
  echo AI Checklist Hub is already running.
  start "" http://127.0.0.1:8000
  exit /b 0
)
echo AI Checklist Hub: http://127.0.0.1:8000
start "" http://127.0.0.1:8000
call .venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
