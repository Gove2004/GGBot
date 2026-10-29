#!/usr/bin/env bash
# 一次性安装：注册 systemd 单元 ggbot.service，并接管原来靠 crontab @reboot
# 拉起的启动方式（两条都留着会抢单实例锁，所以装上后把 @reboot 那行删掉）。
#
#   sudo bash deploy/install_service.sh
set -euo pipefail
cd "$(dirname "$0")/.."

if [ "$(id -u)" -ne 0 ]; then
  echo "需要 root 运行：sudo bash deploy/install_service.sh" >&2
  exit 1
fi

cp deploy/ggbot.service /etc/systemd/system/ggbot.service
systemctl daemon-reload
systemctl enable ggbot.service

# 删掉 crontab 里的 @reboot ... start*.sh 行（只删这一类，别的定时任务不动）
if crontab -l 2>/dev/null | grep -qE '^@reboot.*start(_linux)?\.sh'; then
  crontab -l | grep -vE '^@reboot.*start(_linux)?\.sh' | crontab -
  echo "[deploy] 已移除 crontab 里的 @reboot 启动行"
fi

# 旧的 nohup 进程还在跑就先停，否则新实例会被单实例锁挡在门外
OLD=$(pgrep -f 'main\.py' || true)
if [ -n "$OLD" ]; then
  echo "[deploy] 停止旧进程 PID=${OLD}"
  kill $OLD
  for _ in $(seq 1 15); do pgrep -f 'main\.py' >/dev/null || break; sleep 1; done
fi

systemctl restart ggbot.service
sleep 2
systemctl --no-pager status ggbot.service | head -12
echo "[deploy] 完成：此后用 systemctl status|restart ggbot 管理，或跑 deploy/deploy_server.sh"
