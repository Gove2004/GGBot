#!/usr/bin/env bash
# 部署收尾：在服务器上跑（GitHub Actions 通过 SSH 调用，也可以手动执行）。
#
#   sudo bash deploy/deploy_server.sh     # 用 systemd 托管时（CI 走这条）
#   bash deploy/deploy_server.sh          # 没有 systemd 时退回后台启动
#
# 前提：代码已经同步到项目目录（CI 用 rsync + deploy/rsync-exclude.txt，见 .github/workflows/deploy.yml）。
# 密钥与运行时数据（db/config.json、ggbot.db、temp/、tool 素材）不在同步范围内，
# 所以这里只补环境、装依赖、更新 unit、重启并探活。
set -euo pipefail
cd "$(dirname "$0")/.."
source deploy/service.sh

# CI 的 rsync 排除 .venv/，所以新服务器第一次部署时这个目录还不存在；
# 系统升级掉 Python 后旧 venv 的软链接失效也是同一种情况。先补齐，别让它死在下一句 pip 上。
PY=.venv/bin/python
if [ ! -x "$PY" ] || ! "$PY" --version >/dev/null 2>&1; then
  echo "[deploy] 创建虚拟环境 .venv ..."
  python3 -m venv .venv
fi

# requirements.txt 变过才装依赖，平时几毫秒跳过
NEW=$(sha1sum requirements.txt | cut -d' ' -f1)
if [ ! -f .venv/requirements.sha ] || [ "$(cat .venv/requirements.sha)" != "$NEW" ]; then
  echo "[deploy] 依赖有变化，安装 requirements.txt ..."
  "$PY" -m pip install -q -r requirements.txt
  echo "$NEW" > .venv/requirements.sha
fi

if [ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1; then
  # unit 归仓库管（按本机目录渲染后比对，见 deploy/service.sh），改完 push 即生效，不用重跑 install_service.sh
  if [ "$(id -u)" -ne 0 ]; then
    echo "[deploy] 这台机器用 systemd 托管，更新 unit 与重启服务需要 root：sudo bash deploy/deploy_server.sh" >&2
    exit 1
  fi
  apply_unit
  systemctl restart ggbot.service
  MANAGED=systemd
else
  # 还没跑过 install_service.sh：后台拉起来能救急，但进程掉了没人管
  echo "[deploy] 没检测到 systemd，退回 start_linux.sh 后台启动（掉线不会自动重启，建议 sudo bash deploy/install_service.sh 接管）"
  bash start_linux.sh
  MANAGED=nohup
fi

# 探活：面板开着就探它的首页响应；`web.enable=false` 是故意关的，
# 那种情况改看进程还在不在——以前一律探端口，关掉面板的部署每次都被判成失败。
read -r WEB_ON WEB_PORT <<< "$(web_probe db/config.json "$PY")"

alive() {
  case "$MANAGED" in
    systemd) systemctl is-active --quiet ggbot.service ;;
    *) [ -n "$(bot_pids)" ] ;;
  esac
}

for _ in $(seq 1 30); do
  if [ "$WEB_ON" = on ]; then
    if curl -fsS "http://127.0.0.1:${WEB_PORT}/" >/dev/null 2>&1; then
      echo "[deploy] 面板端口 ${WEB_PORT} 响应正常，重启成功"
      exit 0
    fi
  elif alive; then
    echo "[deploy] 面板按配置关着（web.enable=false），服务进程在跑，重启成功"
    exit 0
  fi
  sleep 1
done

echo "[deploy] 30 秒内没起来（面板=${WEB_ON}:${WEB_PORT}，托管=${MANAGED}），现场如下：" >&2
if [ "$MANAGED" = systemd ]; then
  # systemd 下控制台输出进 journal，temp/logs/stdout.log 是上一次 nohup 留下的旧文件，别再拿它当现场
  systemctl --no-pager status ggbot.service >&2 || true
  journalctl -u ggbot.service -n 40 --no-pager >&2 || true
else
  tail -40 temp/logs/stdout.log >&2 || true
fi
tail -40 temp/logs/ggbot.log >&2 || true
exit 1
