@echo off
cd /d "%~dp0"
set PYTHONUTF8=1

:loop
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" telegram_bot.py
) else (
  python telegram_bot.py
)
echo [%date% %time%] Telegram bot exited, restarting in 30 seconds...
timeout /t 30 /nobreak >nul
goto loop
