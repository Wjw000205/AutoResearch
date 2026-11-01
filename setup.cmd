@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
  if errorlevel 1 exit /b 1
)
".venv\Scripts\python.exe" -m pip install -e ".[test]"
if errorlevel 1 exit /b 1
pushd frontend
if errorlevel 1 exit /b 1
call npm install
if errorlevel 1 goto frontend_failed
call npm run build
if errorlevel 1 goto frontend_failed
popd
echo Ready. Run start.cmd and open http://127.0.0.1:8000
exit /b 0

:frontend_failed
popd
exit /b 1
