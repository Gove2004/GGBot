#!/usr/bin/env bash
# ============================================================
#  GGBot launcher - Linux BACKGROUND mode
#  Auto: create .venv if missing, pip install if deps missing
#  Logs: temp/logs/ggbot.log (rotating 5x5MB)
#  Stop: kill <PID>   (PID printed below)
# ============================================================
set -euo pipefail
cd "$(dirname "$0")"

PY=".venv/bin/python"
# 解释器不可用就重建 venv：apt 升级 Python 后旧 venv 的软链接会指向没了的解释器
if [ ! -x "$PY" ] || ! "$PY" --version >/dev/null 2>&1; then
  echo "[GGBot] creating virtual env .venv ..."
  python3 -m venv .venv
fi

echo "[GGBot] checking dependencies ..."
# probe every third-party import the bot needs, in sync with requirements.txt
if ! "$PY" -c "import botpy, openai, fastapi, uvicorn, numpy, pysilk" >/dev/null 2>&1; then
  echo "[GGBot] installing requirements.txt ..."
  "$PY" -m pip install -r requirements.txt
fi

mkdir -p temp/logs
# 控制台输出每次启动覆盖：ggbot.log 才是留档的那份（轮转 5x5MB），stdout 只是启动失败的兜底
nohup "$PY" main.py --log-level DEBUG > temp/logs/stdout.log 2>&1 &
PID=$!
echo "[GGBot] started in background, PID=${PID}"
echo "[GGBot] logs: temp/logs/ggbot.log (rotating) + temp/logs/stdout.log (console)"
echo "[GGBot] stop: kill ${PID}"
