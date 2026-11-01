@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.cmd first.
  exit /b 1
)
if /i "%~1"=="--foreground" (
  ".venv\Scripts\python.exe" -m uvicorn autoresearch.api:app --host 127.0.0.1 --port 8000
  exit /b
)
".venv\Scripts\python.exe" -m autoresearch.server_runtime %*
