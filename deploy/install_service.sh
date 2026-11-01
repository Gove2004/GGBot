#!/usr/bin/env bash
# 一次性安装：把 systemd 单元装进本机并接管开机自启。
#
#   sudo bash deploy/install_service.sh
#
# 干四件事：按本机项目目录渲染并写入 unit、enable（开机自启）、清掉 crontab 里的
# `@reboot ... start.sh`、停掉还在跑的旧进程后起服务。
# 两条启动路径并存时会抢 db/.bot.lock 单实例锁，谁后起谁进不去，所以必须只留一条。
#
# 装完之后日常部署不用再跑这里，交给 deploy/deploy_server.sh（它每次都会比对并重写 unit）。
set -euo pipefail
cd "$(dirname "$0")/.."
source deploy/service.sh

if [ "$(id -u)" -ne 0 ]; then
  echo "需要 root 运行：sudo bash deploy/install_service.sh" >&2
  exit 1
fi

apply_unit
systemctl enable ggbot.service

# 删掉 crontab 里的 @reboot ... start*.sh 行（只删这一类，别的定时任务不动）
if crontab -l 2>/dev/null | grep -qE '^@reboot.*start(_linux)?\.sh'; then
  crontab -l | grep -vE '^@reboot.*start(_linux)?\.sh' | crontab -
  echo "[deploy] 已移除 crontab 里的 @reboot 启动行"
fi

# 服务已在跑就先干净地停掉，否则新实例会被单实例锁挡在门外
if systemctl is-active --quiet ggbot.service; then
  systemctl stop ggbot.service
fi
# 只停本目录起的那些 main.py（同机别的 python 进程不碰）
OLD=$(bot_pids)
if [ -n "$OLD" ]; then
  echo "[deploy] 停止旧进程 PID=$(echo "$OLD" | tr '\n' ' ')"
  kill $OLD
  for _ in $(seq 1 15); do
    [ -z "$(bot_pids)" ] && break
    sleep 1
  done
fi

systemctl restart ggbot.service
sleep 2
# 服务没起来时 `systemctl status` 返回非零，脚本会被 set -e 截在半路（状态恰恰是这时候要看的）
systemctl --no-pager status ggbot.service | head -12 || true

# 面板能不能开、密钥填没填，是这台机器第一次安装最常卡住的一步，顺手报一句
if [ ! -f db/config.json ]; then
  echo "[deploy] 注意：db/config.json 还没生成，进程第一次启动会从 config.example.json 复制一份，"
  echo "[deploy]       然后需要在里面填 qq.appid / qq.secret（面板登录令牌 web.token 也建议改掉）"
fi
echo "[deploy] 完成：此后用 systemctl status|restart ggbot 管理，或跑 deploy/deploy_server.sh"
