@echo off
rem BurstBuster launcher: first run sets up Python packages, then opens the app.
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo First run: setting up. This takes a few minutes and needs internet once.
  py -3.13 -m venv .venv 2>nul || python -m venv .venv
  if errorlevel 1 (
    echo Could not create the Python environment. Install Python 3.13 from python.org and try again.
    pause
    exit /b 1
  )
  ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
  if errorlevel 1 (
    echo Installing packages failed. See the messages above.
    rmdir /s /q .venv
    pause
    exit /b 1
  )
)

rem Downloads the AI models once (hash-checked); does nothing if they are already there.
".venv\Scripts\python.exe" -c "from burstbuster.models import ensure_models; ensure_models(print)"
if errorlevel 1 (
  echo Downloading the models failed. Check your internet connection and run START.bat again.
  pause
  exit /b 1
)

start "" ".venv\Scripts\pythonw.exe" -m burstbuster
