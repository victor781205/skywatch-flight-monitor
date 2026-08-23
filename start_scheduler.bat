@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" scheduler.py
) else (
  python scheduler.py
)
exit /b %ERRORLEVEL%
