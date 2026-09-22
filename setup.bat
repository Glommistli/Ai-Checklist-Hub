@echo off
setlocal
cd /d "%~dp0"
echo [1/7] Preparing Python environment...
if not exist ".venv\Scripts\python.exe" python -m venv .venv
if errorlevel 1 goto :error
echo [2/7] Installing backend dependencies...
call .venv\Scripts\python.exe -m pip install --upgrade pip
call .venv\Scripts\python.exe -m pip install -r backend\requirements.txt
if errorlevel 1 goto :error
echo [3/7] Installing desktop application dependencies...
call .venv\Scripts\python.exe -m pip install -r desktop\requirements.txt
if errorlevel 1 goto :error
echo [4/7] Initializing web-mode database schema...
pushd backend
call ..\.venv\Scripts\python.exe -m alembic upgrade head
if errorlevel 1 (popd & goto :error)
popd
echo [5/7] Installing frontend dependencies...
pushd frontend
call npm install
if errorlevel 1 (popd & goto :error)
echo [6/7] Building production frontend...
call npm run build
if errorlevel 1 (popd & goto :error)
popd
if not exist "backend\data" mkdir "backend\data"
echo [7/7] Building native Windows application...
call build-desktop.bat
if errorlevel 1 goto :error
echo.
echo Setup complete. Run start-app.bat for the desktop app or start.bat for web mode.
exit /b 0
:error
echo.
echo Setup failed. Review the error above, then run setup.bat again.
exit /b 1
