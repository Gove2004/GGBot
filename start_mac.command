#!/usr/bin/env bash
# ============================================================
#  GGBot launcher - macOS FOREGROUND mode
#  Double-click it, or:  bash start_mac.command
#  (If the executable bit got lost on the way here:  chmod +x start_mac.command)
#  Auto: create .venv if missing, pip install if deps missing
#  Logs: temp/logs/ggbot.log (rotating 5x5MB)
#  Stop: Ctrl+C  (closing the Terminal window also stops the bot)
# ============================================================
set -euo pipefail
cd "$(dirname "$0")"

PY=".venv/bin/python"
# 解释器不可用就（重）建 venv：不只是不存在——brew 升级 Python 后旧 venv 里的软链接会指向没了的解释器
if [ ! -x "$PY" ] || ! "$PY" --version >/dev/null 2>&1; then
  # macOS has no usable python3 until Command Line Tools / Homebrew provides one.
  SYS_PY=""
  for c in python3.13 python3.12 python3.11 python3; do
    if command -v "$c" >/dev/null 2>&1 \
       && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      SYS_PY="$c"
      break
    fi
  done
  if [ -z "$SYS_PY" ]; then
    echo "[GGBot] Python 3.11+ required. Install one:"
    echo "          xcode-select --install   (Command Line Tools)"
    echo "          brew install python@3.12"
    exit 1
  fi
  echo "[GGBot] creating virtual env .venv (with $SYS_PY) ..."
  "$SYS_PY" -m venv .venv
fi

echo "[GGBot] checking dependencies ..."
# probe every third-party import the bot needs, in sync with requirements.txt
if ! "$PY" -c "import botpy, openai, fastapi, uvicorn, numpy, pysilk" >/dev/null 2>&1; then
  echo "[GGBot] installing requirements.txt ..."
  "$PY" -m pip install --upgrade pip
  "$PY" -m pip install -r requirements.txt
fi

mkdir -p temp/logs

echo "[GGBot] starting ... press Ctrl+C to stop"
echo "[GGBot] logs: temp/logs/ggbot.log"
exec "$PY" main.py --log-level DEBUG
