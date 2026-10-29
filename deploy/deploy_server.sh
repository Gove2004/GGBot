#!/usr/bin/env bash
# 部署收尾：在服务器上跑（GitHub Actions 通过 SSH 调用，也可以手动执行）。
#
#   bash deploy/deploy_server.sh
#
# 前提：代码已经同步到 /root/GGBot（CI 用 rsync，见 .github/workflows/deploy.yml）。
# 密钥与运行时数据（db/config.json、ggbot.db、temp/、tool 素材）不在同步范围内，
# 所以这里只处理依赖、重启和健康检查。
set -euo pipefail
cd "$(dirname "$0")/.."

# requirements.txt 变过才装依赖，平时几毫秒跳过
NEW=$(sha1sum requirements.txt | cut -d' ' -f1)
if [ ! -f .venv/requirements.sha ] || [ "$(cat .venv/requirements.sha)" != "$NEW" ]; then
  echo "[deploy] 依赖有变化，安装 requirements.txt ..."
  .venv/bin/python -m pip install -q -r requirements.txt
  echo "$NEW" > .venv/requirements.sha
fi

if systemctl is-active --quiet ggbot.service || [ -f /etc/systemd/system/ggbot.service ]; then
  systemctl restart ggbot.service
else
  # 还没跑过 install_service.sh，退回旧的后台启动方式
  bash start_linux.sh
fi

PORT=$(.venv/bin/python -c "import json;print(json.load(open('db/config.json'))['web'].get('port',11028))")
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:${PORT}/" >/dev/null 2>&1; then
    echo "[deploy] 面板端口 ${PORT} 响应正常，重启成功"
    exit 0
  fi
  sleep 1
done

echo "[deploy] ${PORT} 端口 30 秒内没起来，最后 40 行日志：" >&2
tail -40 temp/logs/stdout.log >&2 || true
exit 1
