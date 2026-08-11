@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
set SCHEDULE_HOURS=0,12
set CHECK_INTERVAL_MINUTES=
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" scheduler.py
) else (
  python scheduler.py
)
