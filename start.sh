#!/usr/bin/env bash
# ============================================================
#  GGBot launcher - Linux BACKGROUND mode
#  Auto: create .venv if missing, pip install if deps missing
#  Logs: logs/ggbot.log (nohup, rotating 5x5MB)
#  Stop: kill <PID>   (PID printed below)
# ============================================================
set -euo pipefail
cd "$(dirname "$0")"

PY=".venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "[GGBot] creating virtual env .venv ..."
  python3 -m venv .venv
  PY=".venv/bin/python"
fi

echo "[GGBot] checking dependencies ..."
if ! "$PY" -c "import botpy, openai, edge_tts, fastapi, uvicorn, numpy" >/dev/null 2>&1; then
  echo "[GGBot] installing requirements.txt ..."
  "$PY" -m pip install -r requirements.txt
fi

mkdir -p logs
nohup "$PY" main.py >> logs/ggbot.log 2>&1 &
PID=$!
echo "[GGBot] started in background, PID=${PID}"
echo "[GGBot] logs: logs/ggbot.log"
echo "[GGBot] stop: kill ${PID}"
