#!/usr/bin/env bash
# 工具目录重组的一次性搬迁：本地和服务器**各跑一次**，可重复执行（搬过的会跳过）。
#
#   bash deploy/migrate_tool_dirs.sh
#
# 为什么需要它：分类目录（tool/<分类>/）与数据目录（config.json、表情素材、参考音频同目录）
# 这次一起改了名，而数据目录不在 rsync 同步范围内（含密钥、体积大），代码同步过去之后
# 旧数据还躺在旧路径上，工具就会当成"没配置/没素材"而自动停用。
#
# 这个脚本只做两类事：把旧目录里的文件搬到新目录、把 config.json 里 `levels` 的旧工具名键
# 改成新名字。目标已存在一律不覆盖，只打印出来让人核对。
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-python3}"
[ -x .venv/bin/python ] && PY=.venv/bin/python
[ -x .venv/Scripts/python.exe ] && PY=.venv/Scripts/python.exe
export PYTHONIOENCODING=utf-8

# 旧目录 -> 现用目录（历次改名逐级对齐，所以更早期的路径也一并接住）
# 表情素材从 tool/media/emoji/ 升到顶层 tool/emoji/，和代码同目录；
# 定时随分类改名（schedule -> remind -> task），简单存储 kvdb 是新增分类，没有旧路径。
pairs="
tool/chat/emoji tool/emoji
tool/chat/audio tool/media/audio
tool/chat/gen tool/media/generate
tool/knowledge/web tool/web
tool/mail tool/email
tool/schedule tool/remind
tool/remind tool/task
tool/media/emoji tool/emoji
tool/memory_fetch tool/memory_search
tool/memory_write tool/memory_add
tool/memory_save tool/memory_add
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

# 单个配置文件也要跟着分类走的时代结束了：clear_context 已删除，旧 tool/knowledge/config.json
# 不再有任何代码读它，只在最后提示一句。

# levels 里的旧工具名键：改名后旧键会静默失效（回到代码默认门槛），所以跟着换。
# 已删除工具的键直接摘掉，不迁到别的名下。
"$PY" - <<'EOF'
import json
from pathlib import Path

RENAME = {
    "manage_permission": "admin_manage",
    "memory_fetch": "memory_search",
    "memory_write": "memory_add",
    "memory_save": "memory_add",
    "send_sticker": "send_emoji",
    "sticker_list": "list_emoji",
    "emoji_list": "list_emoji",
    "sticker_save": "save_emoji",
    "emoji_save": "save_emoji",
    "send_voice_clone": "send_voice",
    "remind_cancel": "task_del",
    "remind_add": "task_add",
    "remind_list": "task_list",
    "remind_del": "task_del",
    "web_search": "search",
    "web_fetch": "fetch",
}
REMOVED = {"clear_context", "send_image", "send_video", "send_text"}

for p in sorted(Path("tool").rglob("config.json")):
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # 读不懂的配置不动它，只报告
        print(f"[migrate] {p} 解析失败，跳过：{e}")
        continue
    levels = data.get("levels") if isinstance(data, dict) else None
    if not isinstance(levels, dict):
        continue
    hit = {old: levels.pop(old) for old in list(levels) if old in RENAME}
    dropped = {old: levels.pop(old) for old in list(levels) if old in REMOVED}
    if not hit and not dropped:
        continue
    for old, val in hit.items():
        levels.setdefault(RENAME[old], val)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    notes = [f"{o}->{RENAME[o]}" for o in hit] + [f"{o}(工具已删)" for o in dropped]
    print(f"[migrate] {p} 的 levels 键：{', '.join(notes)}")
EOF

# 按旧工具名建的配置目录：现在的代码不会再读，只提示，不代删
for d in tool/send_text tool/manage_permission; do
  [ -d "$d" ] && echo "[migrate] 提示：$d/ 还留着，旧工具名建的配置目录已无人读取，确认无用时可删"
done
if [ -f tool/memory/config.json ]; then
  echo "[migrate] 提示：tool/memory/config.json 是旧 clear_context 的配置，该工具已删除，可以删掉"
fi

if [ "$changed" = 0 ]; then
  echo "[migrate] 没有需要搬的目录（已经搬过了）"
fi
echo "[migrate] 完成。重启进程后到 Web「工具」页确认各工具都是启用状态、素材数量正常。"
