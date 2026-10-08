@echo off
REM ============================================================
REM  Lumivara Sentinel - Windows launcher
REM  Python: portable .runtime\python if present, else .venv
REM  (created on first run). Runs the setup wizard when .env is
REM  missing or incomplete, then starts the bot.
REM ============================================================
setlocal
cd /d "%~dp0"

set "PY=%~dp0.runtime\python\python.exe"
if exist "%PY%" goto :deps

set "PY=%~dp0.venv\Scripts\python.exe"
if exist "%PY%" goto :deps

echo [setup] Creating virtual environment...
where py >nul 2>nul && (py -3 -m venv .venv) || (python -m venv .venv)
if not exist "%PY%" (
    echo [error] Python 3.11+ not found. Install it from https://www.python.org/downloads/
    echo [error] ^(tick "Add python.exe to PATH"^) or use the one-line installer in README.
    pause
    exit /b 1
)

:deps
echo [setup] Checking dependencies...
"%PY%" -m pip install --disable-pip-version-check --no-warn-script-location -q -r requirements.txt
if errorlevel 1 (
    echo [error] Could not install dependencies - check your internet connection.
    pause
    exit /b 1
)

"%PY%" -m lumivara.setup_wizard --check
if errorlevel 1 (
    echo [setup] First run - a few questions: Telegram bot + game login
    "%PY%" -m lumivara.setup_wizard
    if errorlevel 1 (
        pause
        exit /b 1
    )
)

echo [run] Starting Lumivara Sentinel...  (press Ctrl+C to stop)
"%PY%" main.py

echo.
echo [stopped] Bot exited. Logs: logs\bot.log
pause
endlocal
