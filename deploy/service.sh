#!/usr/bin/env bash
# 部署脚本共用的一层（被 deploy_server.sh / install_service.sh source，不单独执行）。
#
# 为什么要有这个文件：systemd unit 的路径渲染、"哪个进程才是本项目的机器人"、面板探活口径
# 这三件事，以前在两个脚本里各写一遍，改一处就漏一处——尤其 unit：仓库里那份把项目目录写死成
# /root/GGBot，而 deploy_server.sh 每次部署都拿它覆盖 /etc 里那份，项目放在别的目录的机器
# 一次部署就把服务指到了不存在的路径上。

# 模板里写死的那个目录，渲染时换成本机实际的项目根
UNIT_DEFAULT_ROOT=/root/GGBot
SERVICE_SH_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)

# 按本机项目目录渲染 unit：模板只有 WorkingDirectory / ExecStart 两处带绝对路径
render_unit() {
  local root=${1:-$(pwd -P)}
  sed "s|${UNIT_DEFAULT_ROOT}|${root}|g" "${SERVICE_SH_DIR}/ggbot.service"
}

# 渲染后与 /etc 里那份比对：一样就什么都不做（省一次 daemon-reload，也让每次部署的输出稳定）
apply_unit() {
  local dest=${UNIT_DEST:-/etc/systemd/system/ggbot.service} tmp root
  root=$(pwd -P)
  tmp=$(mktemp)
  render_unit "$root" > "$tmp"
  if [ -f "$dest" ] && cmp -s "$tmp" "$dest"; then
    rm -f "$tmp"
    echo "[unit] ${dest} 已是最新（项目目录 ${root}）"
    return 0
  fi
  cp "$tmp" "$dest"
  rm -f "$tmp"
  systemctl daemon-reload
  echo "[unit] 已按项目目录 ${root} 写入 ${dest} 并 daemon-reload"
}

# 面板该不该探：打印 "on <端口>" 或 "off <端口>"。
# 配置文件还没生成（第一次启动才从模板复制）或读不懂时按默认值算：面板开、端口 11028
web_probe() {
  local cfg=${1:-db/config.json} py=${2:-python3}
  "$py" - "$cfg" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    cfg = json.loads(path.read_text(encoding="utf-8"))
except Exception:  # 没有 / 不是合法 JSON / 缺 web 段——都按代码默认口径走
    cfg = {}
web = (cfg or {}).get("web") or {}
print("on" if web.get("enable", True) else "off", web.get("port", 11028))
PY
}

# 本项目起的 main.py 的 PID，一行一个。
# 只认工作目录就是本仓库的那些：`pgrep -f main.py` 会把同机上任何叫 main.py 的进程算进来，
# 以前 install_service.sh 就这么把无关进程一起 kill 过。
bot_pids() {
  local root=${1:-$(pwd -P)} pid cwd
  for pid in $(pgrep -f 'main\.py' 2>/dev/null || true); do
    cwd=$(readlink -f "/proc/${pid}/cwd" 2>/dev/null) || continue
    [ "$cwd" = "$root" ] && printf '%s\n' "$pid"
  done
}
