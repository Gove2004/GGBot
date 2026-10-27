@echo off
rem ============================================================
rem  GGBot launcher - Windows FOREGROUND mode
rem  Logs: logs\ggbot.log  (rotating, 5x5MB)
rem  Stop: Ctrl+C
rem ============================================================
setlocal
cd /d "%~dp0"
title GGBot QQ Bot [foreground]

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] virtual env not found: .venv
    echo [HINT] python -m venv .venv
    echo [HINT] .venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)

if not exist "logs" mkdir "logs"

echo [GGBot] starting ... log file: logs\ggbot.log
echo [GGBot] press Ctrl+C to stop
".venv\Scripts\python.exe" main.py %*
set "EXIT_CODE=%ERRORLEVEL%"
echo [GGBot] exited with code %EXIT_CODE%
pause
exit /b %EXIT_CODE%
