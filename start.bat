@echo off
rem ============================================================
rem  GGBot launcher - Windows FOREGROUND mode
rem  Auto: create .venv if missing, pip install if deps missing
rem  Logs: temp\logs\ggbot.log  (rotating, 5x5MB)
rem  Stop: Ctrl+C
rem ============================================================
setlocal
cd /d "%~dp0"
title GGBot QQ Bot [foreground]

if not exist ".venv\Scripts\python.exe" (
    echo [GGBot] creating virtual env .venv ...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] failed to create .venv, install Python 3 first
        pause
        exit /b 1
    )
)

echo [GGBot] checking dependencies ...
".venv\Scripts\python.exe" -c "import botpy, openai, edge_tts, fastapi, uvicorn, numpy" >nul 2>&1
if errorlevel 1 (
    echo [GGBot] installing requirements.txt ...
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] pip install failed
        pause
        exit /b 1
    )
)

if not exist "temp" mkdir "temp"

echo [GGBot] starting ...
echo [GGBot] press Ctrl+C to stop
".venv\Scripts\python.exe" main.py --log-level DEBUG
set "EXIT_CODE=%ERRORLEVEL%"
echo [GGBot] exited with code %EXIT_CODE%
pause
exit /b %EXIT_CODE%
