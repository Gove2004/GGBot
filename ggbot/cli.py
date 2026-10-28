"""GGBot 运维 CLI：会话/上下文/记忆管理。

用法（在项目根目录）：
    .venv\\Scripts\\python -m ggbot.cli stats
    .venv\\Scripts\\python -m ggbot.cli sessions
    .venv\\Scripts\\python -m ggbot.cli context 群1A2B3C4D
    .venv\\Scripts\\python -m ggbot.cli clear 群1A2B3C4D --yes

    .venv\\Scripts\\python -m ggbot.cli memory list [--session 群1A2B3C4D]
    .venv\\Scripts\\python -m ggbot.cli memory search 喜欢的食物
    .venv\\Scripts\\python -m ggbot.cli memory add "小明喜欢喝美式" --importance 3
    .venv\\Scripts\\python -m ggbot.cli memory edit <id> --content "新的内容"
    .venv\\Scripts\\python -m ggbot.cli memory delete <id> --yes
    .venv\\Scripts\\python -m ggbot.cli memory tidy --yes

会话引用支持：完整 id（group:... / c2c:...）、8 位标签（群1A2B3C4D / 私聊1A2B3C4D）、
openid 前缀（1A2B3C4D）。配置了 embedding 时记忆检索为语义检索，否则按关键词匹配。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:  # Windows 控制台默认 GBK，统一按 UTF-8 输出
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

from ggbot.core.config import Config  # noqa: E402
from ggbot.core.storage import Storage  # noqa: E402
from ggbot.llm.memory import MemoryStore  # noqa: E402
from ggbot.util.session import session_label  # noqa: E402


def _clip(s: str | None, n: int = 48) -> str:
    s = (s or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _confirm(prompt: str) -> bool:
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes", "是")
    except EOFError:
        return False


def _resolve_session(storage: Storage, ref: str) -> str:
    ref = (ref or "").strip()
    if not ref:
        raise SystemExit("会话引用不能为空")
    sessions = storage.list_sessions(1000)
    for s in sessions:
        if s["id"] == ref:
            return ref
    matches = [
        s["id"]
        for s in sessions
        if session_label(s["id"]) == ref or s["id"].split(":", 1)[-1].startswith(ref)
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise SystemExit(f"未找到会话: {ref}")
    print("会话引用不唯一，请使用完整 id：")
    for sid in matches:
        print(f"  {sid}  ({session_label(sid)})")
    raise SystemExit(2)


def _print_sessions(rows: list[dict], live: dict | None = None) -> None:
    if not rows:
        print("（暂无会话）")
        return
    live = live or {}
    print(f"{'会话':<14} {'状态':<6} {'消息':>6} {'待处理':>6}  {'最后活动':<19} 摘要 / 最后消息")
    print("-" * 110)
    for r in rows:
        st = live.get(r["id"], {})
        print(
            f"{session_label(r['id']):<14} {st.get('state', 'offline'):<6} "
            f"{r.get('message_count') or 0:>6} {r.get('pending_count') or 0:>6}  "
            f"{(r.get('last_ts') or r.get('updated_at') or '—'):<19} "
            f"{_clip(r.get('summary') or r.get('last_message'))}"
        )


def cmd_stats(storage: Storage) -> None:
    s = storage.stats()
    print("GGBot 数据概览")
    print(f"  会话      {s['sessions']}")
    print(f"  消息      {s['messages']}（今日 {s['messages_today']}）")
    print(f"  待处理    {s['pending']}")
    print(f"  记忆      {s['memories']}")
    print(f"  规则      {s['rules']}")


def cmd_sessions(storage: Storage, limit: int) -> None:
    _print_sessions(storage.list_sessions(limit))


def cmd_context(storage: Storage, ref: str, limit: int) -> None:
    sid = _resolve_session(storage, ref)
    row = storage.get_session(sid)
    print(f"会话 {sid}（{session_label(sid)}）")
    if not row:
        raise SystemExit("会话不存在")
    print(f"消息 {row.get('message_count')}，待处理 {row.get('pending_count')}，更新于 {row.get('updated_at')}")
    summary = storage.get_summary(sid)
    if summary:
        print(f"\n[历史摘要]\n{summary}\n")
    print(f"[最近 {limit} 条]")
    for m in storage.get_recent_messages(sid, limit):
        role = "机器人" if m.get("role") == "assistant" else "成员"
        flag = " [待处理]" if m.get("status") == "pending" else ""
        print(f"{m.get('ts')}  {role:<4} {_clip(m.get('content'), 80)}{flag}")


def cmd_clear(storage: Storage, ref: str, yes: bool) -> None:
    sid = _resolve_session(storage, ref)
    if not yes and not _confirm(f"清空会话 {sid} 的消息与历史摘要？"):
        print("已取消")
        return
    removed = storage.clear_session_context(sid)
    print(f"已清空 {sid}：{removed['messages']} 条消息，历史摘要已重置")


def cmd_reset_chat(storage: Storage, yes: bool) -> None:
    if not yes and not _confirm("清空全部会话与消息（保留记忆/规则/配置）？"):
        print("已取消")
        return
    n = storage.reset_chat_data()
    print(f"已清理 messages={n['messages']} sessions={n['sessions']}（记忆/规则保留）")


def cmd_memory_list(storage: Storage, sid: str | None, limit: int) -> None:
    rows = storage.list_memories(sid, limit)
    if not rows:
        print("（暂无记忆）")
        return
    print(f"{'ID':<17} {'重要':>3} {'会话':<14} {'时间':<19} 内容")
    print("-" * 110)
    for m in rows:
        scope = session_label(m["session_id"]) if m.get("session_id") else "全局"
        print(
            f"{m['id']:<17} {int(m.get('importance') or 1):>3} {scope:<14} "
            f"{(m.get('ts') or '—'):<19} {_clip(m.get('content'), 60)}"
        )


def cmd_memory_search(memory: MemoryStore, query: str, k: int, sid: str | None) -> None:
    hits = memory.search(query, k, sid, None)
    if not hits:
        print("没有找到相关记忆")
        return
    for h in hits:
        scope = session_label(h["session_id"]) if h.get("session_id") else "全局"
        print(f"{h['score']:.4f}  [{scope}]  {h['content']}")


def cmd_memory_add(memory: MemoryStore, content: str, importance: int, sid: str | None) -> None:
    mid = memory.add(content, "fact", importance, sid)
    print(f"已写入记忆 {mid}")


def cmd_memory_edit(memory: MemoryStore, mid: str, content: str | None, importance: int | None) -> None:
    if content is None and importance is None:
        raise SystemExit("需要 --content 或 --importance")
    try:
        ok = memory.update(mid, content, importance)
    except ValueError as e:
        raise SystemExit(str(e)) from e
    print(f"已更新记忆 {mid}" if ok else f"记忆不存在: {mid}")


def cmd_memory_delete(storage: Storage, mid: str, yes: bool) -> None:
    if not storage.get_memory(mid):
        raise SystemExit(f"记忆不存在: {mid}")
    if not yes and not _confirm(f"删除记忆 {mid}？"):
        print("已取消")
        return
    storage.delete_memory(mid)
    print(f"已删除记忆 {mid}")


def cmd_memory_tidy(memory: MemoryStore, threshold: float, yes: bool) -> None:
    if not yes and not _confirm(f"合并相似度 ≥ {threshold} 的重复记忆？"):
        print("已取消")
        return
    removed = memory.tidy(threshold)
    print(f"整理完成，合并删除 {removed} 条")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m ggbot.cli",
                                description="GGBot 会话 / 上下文 / 记忆管理")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("stats", help="数据概览")

    sp = sub.add_parser("sessions", help="会话列表")
    sp.add_argument("--limit", type=int, default=200)

    sp = sub.add_parser("context", help="查看某会话上下文")
    sp.add_argument("session")
    sp.add_argument("--limit", type=int, default=30)

    sp = sub.add_parser("clear", help="清空某会话上下文（保留记忆）")
    sp.add_argument("session")
    sp.add_argument("--yes", action="store_true")

    sp = sub.add_parser("reset-chat", help="清空全部会话与消息（保留记忆/规则）")
    sp.add_argument("--yes", action="store_true")

    mem = sub.add_parser("memory", help="记忆管理")
    msub = mem.add_subparsers(dest="mem_cmd", required=True)

    sp = msub.add_parser("list", help="列出记忆")
    sp.add_argument("--session", default=None)
    sp.add_argument("--limit", type=int, default=50)

    sp = msub.add_parser("search", help="语义检索记忆")
    sp.add_argument("query")
    sp.add_argument("--k", type=int, default=5)
    sp.add_argument("--session", default=None)

    sp = msub.add_parser("add", help="写入记忆")
    sp.add_argument("content")
    sp.add_argument("--importance", type=int, default=1, choices=range(1, 6))
    sp.add_argument("--session", default=None)

    sp = msub.add_parser("edit", help="编辑记忆（改内容会重新向量化）")
    sp.add_argument("memory_id")
    sp.add_argument("--content", default=None)
    sp.add_argument("--importance", type=int, default=None, choices=range(1, 6))

    sp = msub.add_parser("delete", help="删除记忆")
    sp.add_argument("memory_id")
    sp.add_argument("--yes", action="store_true")

    sp = msub.add_parser("tidy", help="合并近似重复记忆")
    sp.add_argument("--threshold", type=float, default=0.95)
    sp.add_argument("--yes", action="store_true")

    return p


def main() -> None:
    args = build_parser().parse_args()
    cfg = Config()
    storage = Storage(cfg.db_path)
    try:
        if args.cmd == "stats":
            cmd_stats(storage)
        elif args.cmd == "sessions":
            cmd_sessions(storage, args.limit)
        elif args.cmd == "context":
            cmd_context(storage, args.session, args.limit)
        elif args.cmd == "clear":
            cmd_clear(storage, args.session, args.yes)
        elif args.cmd == "reset-chat":
            cmd_reset_chat(storage, args.yes)
        elif args.cmd == "memory":
            sid = _resolve_session(storage, args.session) if getattr(args, "session", None) else None
            if args.mem_cmd == "list":
                cmd_memory_list(storage, sid, args.limit)
                return
            if args.mem_cmd == "delete":
                cmd_memory_delete(storage, args.memory_id, args.yes)
                return
            memory = MemoryStore(cfg, storage)
            if args.mem_cmd == "search":
                cmd_memory_search(memory, args.query, args.k, sid)
            elif args.mem_cmd == "add":
                cmd_memory_add(memory, args.content, args.importance, sid)
            elif args.mem_cmd == "edit":
                cmd_memory_edit(memory, args.memory_id, args.content, args.importance)
            elif args.mem_cmd == "tidy":
                cmd_memory_tidy(memory, args.threshold, args.yes)
    finally:
        storage.close()


if __name__ == "__main__":
    main()
