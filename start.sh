#!/usr/bin/env bash
# ============================================================
#  GGBot launcher - Linux BACKGROUND mode
#  Logs: logs/ggbot.log (nohup, rotating 5x5MB)
#  Stop: kill <PID>   (PID printed below)
# ============================================================
set -euo pipefail
cd "$(dirname "$0")"

PY=".venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "[ERROR] virtual env not found: .venv"
  echo "[HINT] python3 -m venv .venv"
  echo "[HINT] .venv/bin/pip install -r requirements.txt"
  exit 1
fi

mkdir -p logs
nohup "$PY" main.py "$@" >> logs/ggbot.log 2>&1 &
PID=$!
echo "[GGBot] started in background, PID=${PID}"
echo "[GGBot] logs: logs/ggbot.log"
echo "[GGBot] stop: kill ${PID}"
