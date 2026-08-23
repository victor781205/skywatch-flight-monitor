@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m streamlit run app.py --server.address 127.0.0.1 --server.port 8501 --server.headless true --browser.gatherUsageStats false
) else (
  python -m streamlit run app.py --server.address 127.0.0.1 --server.port 8501 --server.headless true --browser.gatherUsageStats false
)
