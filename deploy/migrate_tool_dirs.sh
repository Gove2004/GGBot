#!/usr/bin/env bash
# 工具目录重组的一次性搬迁：本地和服务器**各跑一次**，可重复执行（搬过的会跳过）。
#
#   bash deploy/migrate_tool_dirs.sh
#
# 为什么需要它：分类目录（tool/<分类>/）与数据目录（config.json、表情素材、参考音频同目录）
# 这次一起改了名，而数据目录不在 rsync 同步范围内（含密钥、体积大），代码同步过去之后
# 旧数据还躺在旧路径上，工具就会当成"没配置/没素材"而自动停用。
#
# 这个脚本只做三类事：把旧目录里的文件搬到新目录、把按旧工具名建的配置目录改名、
# 把 config.json 里 `levels` 的旧工具名键改成新名字。目标已存在一律不覆盖，只打印出来让人核对。
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-python3}"
[ -x .venv/bin/python ] && PY=.venv/bin/python
[ -x .venv/Scripts/python.exe ] && PY=.venv/Scripts/python.exe
export PYTHONIOENCODING=utf-8

# 旧目录 -> 新目录（逐项搬文件，不动目标里已有的东西）
pairs="
tool/chat/emoji tool/media/emoji
tool/chat/audio tool/media/audio
tool/chat/gen tool/media/generate
tool/knowledge/web tool/web
tool/memory_search tool/memory_fetch
tool/memory_write tool/memory_save
"

changed=0
while read -r from to; do
  [ -z "${from:-}" ] && continue
  if [ ! -d "$from" ]; then
    continue
  fi
  mkdir -p "$to"
  for item in "$from"/* "$from"/.*; do
    base=$(basename "$item")
    case "$base" in .|..) continue ;; esac
    [ -e "$item" ] || continue
    if [ -e "$to/$base" ]; then
      echo "[migrate] 跳过 $to/$base（目标已存在，源文件留在 $item）"
      continue
    fi
    mv "$item" "$to/"
    echo "[migrate] $item -> $to/$base"
    changed=1
  done
  # 目录空了才删，还剩文件就说明有冲突，留着让人处理
  if [ -z "$(ls -A "$from" 2>/dev/null)" ]; then
    rmdir "$from"
    echo "[migrate] 空目录 $from 已删除"
  else
    echo "[migrate] $from 里还有没搬走的东西，保留不动"
  fi
done <<< "$pairs"

# 单个配置文件也要跟着分类走（旧 clear_context 的配置键是 knowledge，新键是 memory）
files="
tool/knowledge/config.json tool/memory
"

while read -r from to; do
  [ -z "${from:-}" ] && continue
  [ -f "$from" ] || continue
  mkdir -p "$to"
  if [ -e "$to/$(basename "$from")" ]; then
    echo "[migrate] 跳过 $to/$(basename "$from")（目标已存在，源文件留在 $from）"
    continue
  fi
  mv "$from" "$to/"
  echo "[migrate] $from -> $to/$(basename "$from")"
  changed=1
done <<< "$files"

# levels 里的旧工具名键：改名后旧键会静默失效（回到代码默认门槛），所以跟着换
"$PY" - <<'EOF'
import json
from pathlib import Path

RENAME = {"memory_search": "memory_fetch", "memory_write": "memory_save"}
paths = list(Path("tool").rglob("config.json"))
for p in paths:
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # 读不懂的配置不动它，只报告
        print(f"[migrate] {p} 解析失败，跳过：{e}")
        continue
    levels = data.get("levels") if isinstance(data, dict) else None
    if not isinstance(levels, dict):
        continue
    hit = {old: levels.pop(old) for old in list(levels) if old in RENAME}
    if not hit:
        continue
    for old, val in hit.items():
        levels.setdefault(RENAME[old], val)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[migrate] {p} 的 levels 键改名：{', '.join(f'{o}->{RENAME[o]}' for o in hit)}")
EOF

# 正文通道已经不是工具，按旧工具名留下的配置目录不会再被读取
if [ -d tool/send_text ]; then
  echo "[migrate] 提示：tool/send_text/ 是旧的内部工具配置，发正文已迁到 ggbot/core/outbound.py，这个目录可以直接删"
fi

if [ "$changed" = 0 ]; then
  echo "[migrate] 没有需要搬的目录（已经搬过了）"
fi
echo "[migrate] 完成。重启进程后到 Web「工具」页确认各工具都是启用状态、素材数量正常。"
