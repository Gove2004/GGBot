"""GGBot 运维 CLI：会话/上下文/记忆管理。

用法（在项目根目录）：
    .venv\\Scripts\\python -m ggbot.cli stats
    .venv\\Scripts\\python -m ggbot.cli sessions
    .venv\\Scripts\\python -m ggbot.cli context 群1A2B3C4D
    .venv\\Scripts\\python -m ggbot.cli clear 群1A2B3C4D --yes

    .venv\\Scripts\\python -m ggbot.cli memory list [--session 群1A2B3C4D] [--subject 小明]
    .venv\\Scripts\\python -m ggbot.cli memory search 喜欢的食物
    .venv\\Scripts\\python -m ggbot.cli memory add "小明喜欢喝美式" --subject 小明 --importance 3
    .venv\\Scripts\\python -m ggbot.cli memory add "周五前交稿" --kind event --subject 小明 --expires-days 5
    .venv\\Scripts\\python -m ggbot.cli memory edit <id> --content "新的内容"
    .venv\\Scripts\\python -m ggbot.cli memory delete <id> --yes
    .venv\\Scripts\\python -m ggbot.cli memory purge --subject 小明 --yes
    .venv\\Scripts\\python -m ggbot.cli memory sweep --yes

    .venv\\Scripts\\python -m ggbot.cli task list [--status all] [--session 群1A2B3C4D] [--q 交稿]
    .venv\\Scripts\\python -m ggbot.cli task show 12
    .venv\\Scripts\\python -m ggbot.cli task cancel 12 --yes
    .venv\\Scripts\\python -m ggbot.cli task delete 12 --yes
    .venv\\Scripts\\python -m ggbot.cli task purge --status fired --yes
    .venv\\Scripts\\python -m ggbot.cli task sweep --yes

会话引用支持：完整 id（group:... / c2c:...）、8 位标签（群1A2B3C4D / 私聊1A2B3C4D）、
openid 前缀（1A2B3C4D）。配置了 embedding 时记忆检索为语义检索，否则按关键词匹配。
记忆分两类：profile（某个人的当前状态，同一件事的新说法改写旧的）、
event（发生过的一件事，到期或被推翻就消失）。
定时任务分两种：一次性，和带周期的（每天/工作日/每周/每月/每隔多久）——排任务本身只在对话里做，
CLI 与面板都只能查看、改时间与指令、取消与清理历史。
"""

from __future__ import annotations

import argparse
import sys
import time
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
from ggbot.llm.memory import (IMPORTANCE_MAX, IMPORTANCE_MIN, KIND_LABELS,  # noqa: E402
                              MemoryStore)
from ggbot.util.session import session_label  # noqa: E402


def _clip(s: str | None, n: int = 48) -> str:
    s = (s or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


# 消息的回复结果与出站走的路，终端里说人话（已回过的不标，历史行不必逐条喊「成功」）
_REPLY_FLAG = {"waiting": " [待处理]", "failed": " [回复失败]", "skipped": " [没接话]"}
_SEND_VIA = {"passive": "被动回复", "proactive": "主动发送", "fallback": "降级主动"}


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
    print(f"{'会话':<14} {'状态':<6} {'消息':>6} {'待处理':>6}  {'最后活动':<19} 最后消息")
    print("-" * 110)
    for r in rows:
        st = live.get(r["id"], {})
        print(
            f"{session_label(r['id']):<14} {st.get('state', 'offline'):<6} "
            f"{r.get('message_count') or 0:>6} {r.get('waiting_count') or 0:>6}  "
            f"{(r.get('last_ts') or r.get('updated_at') or '—'):<19} "
            f"{_clip(r.get('last_message'))}"
        )


def cmd_stats(storage: Storage) -> None:
    s = storage.stats()
    print("GGBot 数据概览")
    print(f"  会话      {s['sessions']}")
    print(f"  消息      {s['messages']}（今日 {s['messages_today']}）")
    print(f"  待处理    {s['waiting']}")
    print(f"  记忆      {s['memories']}")
    print(f"  待跑任务 {s.get('tasks_pending', 0)}")


def cmd_sessions(storage: Storage, limit: int) -> None:
    _print_sessions(storage.list_sessions(limit))


def cmd_context(storage: Storage, ref: str, limit: int) -> None:
    sid = _resolve_session(storage, ref)
    row = storage.get_session(sid)
    print(f"会话 {sid}（{session_label(sid)}）")
    if not row:
        raise SystemExit("会话不存在")
    print(f"消息 {row.get('message_count')}，待处理 {row.get('waiting_count')}，更新于 {row.get('updated_at')}")
    print(f"[最近 {limit} 条]")
    for m in storage.get_recent_messages(sid, limit):
        role = "机器人" if m.get("role") == "assistant" else "成员"
        flag = _REPLY_FLAG.get(m.get("reply_state")) or ""
        print(f"{m.get('ts')}  {role:<4} {_clip(m.get('content'), 80)}{flag}")
    sends = storage.recent_sends(sid, 5)
    if sends:
        print("[最近出站]")
        for s in sends:
            print(f"{s.get('ts')}  {_SEND_VIA.get(s.get('via') or '', s.get('via') or '发送'):<6} "
                  f"{_clip(s.get('kind'), 12):<14} {'已送达' if s.get('ok') else '被拒：' + _clip(s.get('detail'), 60)}")


def cmd_clear(storage: Storage, ref: str, yes: bool) -> None:
    sid = _resolve_session(storage, ref)
    if not yes and not _confirm(f"清空会话 {sid} 的消息与出站记录？"):
        print("已取消")
        return
    removed = storage.clear_session_context(sid)
    print(f"已清空 {sid}：{removed['messages']} 条消息，归档进度与出站记录已重置")


def cmd_reset_chat(storage: Storage, yes: bool) -> None:
    if not yes and not _confirm("清空全部会话与消息（保留记忆/配置）？"):
        print("已取消")
        return
    n = storage.reset_chat_data()
    print(f"已清理 messages={n['messages']} sessions={n['sessions']}（记忆保留）")


def cmd_memory_list(storage: Storage, memory: MemoryStore, sid: str | None, limit: int,
                   subject: str | None, kind: str | None) -> None:
    rows = memory.panel_rows(storage.list_memories(sid, limit, 0, subject, kind))
    if not rows:
        print("（暂无记忆）")
        return
    print(f"{'ID':<17} {'类':<8} {'归属':<10} {'重要':>3} {'保留':>5} {'命中':>4} {'安静天':>6} "
          f"{'到期':<11} 内容")
    print("-" * 126)
    for m in rows:
        scope = m.get("subject") or "—"
        exp = (m.get("expires_at") or "—")[:10]
        if m.get("expired"):
            exp += "(已过)"
        print(
            f"{m['id']:<17} {m['kind']:<8} {_clip(scope, 10):<10} "
            f"{int(m.get('importance') or 1):>3} {m['retention']:>5.2f} "
            f"{int(m.get('hit_count') or 0):>4} "
            f"{m['quiet_days']:>6.0f} {exp:<11} {_clip(m.get('content'), 46)}"
        )


def cmd_memory_search(memory: MemoryStore, query: str, k: int, sid: str | None) -> None:
    hits = memory.search(query, k, sid, None)
    if not hits:
        print("没有找到相关记忆")
        return
    for h in hits:
        subject = f"/{h['subject']}" if h.get("subject") else ""
        print(f"{h['score']:.4f}  [{h['kind']}{subject}]  {h['content']}")


def cmd_memory_add(memory: MemoryStore, content: str, kind: str, subject: str | None,
                   importance: int, sid: str | None, expires_days: int | None) -> None:
    out = memory.commit([{
        "content": content,
        "kind": kind,
        "subject": subject or "",
        "importance": importance,
        "session_id": sid,
        "expires_in_days": expires_days,
    }])
    if out["written"]:
        print("已写入记忆")
    elif out["updated"]:
        print("已改写库里已有的那条记忆")
    else:
        print("库里已经有这条说法，没有重复写")
    if out["retired"]:
        print(f"顺带作废了 {out['retired']} 条被推翻的旧记忆")


def cmd_memory_edit(memory: MemoryStore, mid: str, content: str | None, importance: int | None,
                    subject: str | None, kind: str | None, expires: str | None) -> None:
    if all(v is None for v in (content, importance, subject, kind, expires)):
        raise SystemExit("需要 --content / --importance / --subject / --kind / --expires")
    if kind is not None and kind not in KIND_LABELS:
        raise SystemExit(f"kind 只能是 {' 或 '.join(KIND_LABELS)}")
    try:
        ok = memory.update(mid, content, importance, subject, kind, expires)
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


def cmd_memory_purge(storage: Storage, subject: str | None, kind: str | None,
                     sid: str | None, yes: bool) -> None:
    if not any((subject, kind, sid)):
        raise SystemExit("需要 --subject / --kind / --session 至少一个条件（清空整库请用 main.py --reset-db）")
    label = " / ".join(str(x) for x in (subject, kind, sid) if x)
    if not yes and not _confirm(f"删除匹配 {label} 的全部记忆？"):
        print("已取消")
        return
    print(f"已删除 {storage.delete_memories_by(sid, subject, kind)} 条记忆")


def cmd_memory_sweep(memory: MemoryStore, threshold: float | None, yes: bool) -> None:
    label = f"≥ {threshold}" if threshold is not None else "自动（随记忆条数放宽 0.95→0.86）"
    if not yes and not _confirm(
        f"遗忘扫描：删掉到期与淡出的事件、按上限淘汰最弱的、合并相似度 {label} 的重复记忆？"):
        print("已取消")
        return
    r = memory.sweep(threshold)
    print(f"到期 {r['expired']} · 淡出 {r['faded']} · 超限 {r['over_cap']} · "
          f"合并 {r['merged']}（剩 {r['total']} 条）")


def _tasks_for_cli(cfg: Config, storage: Storage):
    """任务的操作面：配置读的是 tool/task/config.json，状态全在 tasks 表里。"""
    from tool.base import ToolRuntime
    from tool.task.task import Tasks

    return Tasks(ToolRuntime(cfg, storage, None, None))


def cmd_task_list(storage: Storage, sid: str | None, status: str | None,
                  limit: int, q: str | None) -> None:
    from tool.task._rule import fmt_due, rule_label, span_text

    rows = storage.list_tasks(sid, status, limit, 0, q)
    if not rows:
        print("（暂无任务）")
        return
    now = int(time.time())
    print(f"{'ID':>6} {'会话':<12} {'状态':<9} {'周期':<9} {'已跑':>5} {'到期':<17} "
          f"{'距今':>8} {'谁排的':<8} 指令")
    print("-" * 130)
    for r in rows:
        gap = int(r["due_ts"]) - now
        times = int(r.get("times") or 1)
        fired = int(r.get("fired_count") or 0)
        fires = f"{fired}/{times}" if r.get("rule") and times > 0 else str(fired)
        fails = f" 失败{int(r.get('fail_count') or 0)}" if int(r.get("fail_count") or 0) else ""
        print(f"{r['id']:>6} {session_label(r['session_id']):<12} {r['status']:<9} "
              f"{rule_label(r.get('rule') or ''):<9} {fires:>5} {fmt_due(r['due_ts']):<17} "
              f"{span_text(abs(gap)) + ('后' if gap >= 0 else '前'):>8} "
              f"{_clip(r.get('owner_name') or '—', 8):<8} {_clip(r.get('instruction'), 40)}{fails}")


def cmd_task_show(storage: Storage, rid: int) -> None:
    from tool.task._rule import fmt_due, rule_label

    row = storage.get_task(rid)
    if not row:
        raise SystemExit(f"任务不存在: {rid}")
    times = int(row.get("times") or 1)
    print(f"#{row['id']}  {row['status']}  {session_label(row['session_id'])}")
    print(f"  到期    {fmt_due(row['due_ts'])}")
    print(f"  重复    {rule_label(row.get('rule') or '')}"
          + (f"（已跑 {int(row.get('fired_count') or 0)} 场，"
             + (f"计划 {times} 场）" if times > 0 else "不限场次）") if row.get("rule") else ""))
    print(f"  指令    {row['instruction']}")
    print(f"  谁排的  {row.get('owner_name') or '—'}")
    print(f"  创建    {row.get('created_at') or '—'}   落定 {row.get('fired_at') or '—'}"
          f"   最后一场 {row.get('last_fired_at') or '—'}")
    if int(row.get("fail_count") or 0):
        print(f"  失败    {row['fail_count']} 次：{row.get('last_error') or '—'}")


def cmd_task_cancel(storage: Storage, rid: int, yes: bool) -> None:
    row = storage.get_task(rid)
    if not row:
        raise SystemExit(f"任务不存在: {rid}")
    if row["status"] != "pending":
        raise SystemExit(f"#{rid} 已经结束了（{row['status']}），取消不了")
    from tool.task._rule import fmt_due, rule_label

    tail = f"（整条{rule_label(row['rule'])}任务，已跑 {int(row.get('fired_count') or 0)} 场）" if row["rule"] else ""
    if not yes and not _confirm(f"取消 #{rid} {fmt_due(row['due_ts'])} {row['instruction']}{tail}？"):
        print("已取消操作")
        return
    storage.finish_task(rid, "cancelled")
    print(f"已取消 #{rid}，到点不会再跑")


def cmd_task_delete(storage: Storage, rid: int, yes: bool) -> None:
    row = storage.get_task(rid)
    if not row:
        raise SystemExit(f"任务不存在: {rid}")
    if not yes and not _confirm(f"删除 #{rid}（{row['status']}）这条记录？"):
        print("已取消操作")
        return
    storage.delete_task(rid)
    print(f"已删除 #{rid}")


def cmd_task_purge(storage: Storage, status: str | None, sid: str | None,
                   days: int | None, yes: bool) -> None:
    if status == "pending":
        raise SystemExit("待触发的行不能批量删，请用 task cancel")
    if not any((status, sid, days)):
        raise SystemExit("需要 --status / --session / --before-days 至少一个条件")
    label = " / ".join(str(x) for x in (status, sid, f"{days}天前" if days else None) if x)
    if not yes and not _confirm(f"删除匹配 {label} 的历史任务（待触发的不算）？"):
        print("已取消操作")
        return
    before_ts = int(time.time()) - days * 86400 if days else None
    print(f"已删除 {storage.purge_tasks(sid, status, before_ts)} 条历史任务")


def cmd_task_sweep(tasks, yes: bool) -> None:
    cfg = tasks.config
    if not yes and not _confirm(
        f"清理历史：终态行超过 {cfg.get('keep_history_days')} 天的删掉，"
        f"整表超 {cfg.get('max_rows')} 行再从最旧的删起？"):
        print("已取消操作")
        return
    r = tasks.sweep()
    print(f"到期删 {r['aged']} · 超限删 {r['over']}（剩 {r['total']} 条，保留 {r['keep_days']} 天）")


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

    sp = sub.add_parser("reset-chat", help="清空全部会话与消息（保留记忆）")
    sp.add_argument("--yes", action="store_true")

    mem = sub.add_parser("memory", help="记忆管理")
    msub = mem.add_subparsers(dest="mem_cmd", required=True)

    sp = msub.add_parser("list", help="列出记忆（含归属、命中、到期与保留分）")
    sp.add_argument("--session", default=None)
    sp.add_argument("--subject", default=None, help="只看某个人的档案")
    sp.add_argument("--kind", default=None, choices=list(KIND_LABELS))
    sp.add_argument("--limit", type=int, default=50)

    sp = msub.add_parser("search", help="语义检索记忆")
    sp.add_argument("query")
    sp.add_argument("--k", type=int, default=5)
    sp.add_argument("--session", default=None)

    sp = msub.add_parser("add", help="写入记忆（先和库里相关的旧条目比，判新增还是改写）")
    sp.add_argument("content")
    sp.add_argument("--kind", default="profile", choices=list(KIND_LABELS),
                    help="profile=某人的当前状态，event=发生过的一件事")
    sp.add_argument("--subject", default=None, help="这条是关于谁的")
    sp.add_argument("--importance", type=int, default=IMPORTANCE_MIN, choices=range(IMPORTANCE_MIN, IMPORTANCE_MAX + 1))
    sp.add_argument("--session", default=None)
    sp.add_argument("--expires-days", type=int, default=None, dest="expires_days",
                    help="多少天后过期（只给自带截止日的事）")

    sp = msub.add_parser("edit", help="编辑记忆（改内容会重新向量化）")
    sp.add_argument("memory_id")
    sp.add_argument("--content", default=None)
    sp.add_argument("--importance", type=int, default=None, choices=range(IMPORTANCE_MIN, IMPORTANCE_MAX + 1))
    sp.add_argument("--subject", default=None)
    sp.add_argument("--kind", default=None, choices=list(KIND_LABELS))
    sp.add_argument("--expires", default=None, help="到期时间 YYYY-MM-DD；传空串取消到期")

    sp = msub.add_parser("delete", help="删除记忆")
    sp.add_argument("memory_id")
    sp.add_argument("--yes", action="store_true")

    sp = msub.add_parser("purge", help="整组删除（按人 / 按类别 / 按会话）")
    sp.add_argument("--subject", default=None)
    sp.add_argument("--kind", default=None, choices=list(KIND_LABELS))
    sp.add_argument("--session", default=None)
    sp.add_argument("--yes", action="store_true")

    sp = msub.add_parser("sweep", help="遗忘扫描：到期/淡出/超上限删除 + 合并近似重复")
    sp.add_argument("--threshold", type=float, default=None,
                    help="合并相似度阈值；不填按记忆条数自动放宽（0.95 起，最低 0.86）")
    sp.add_argument("--yes", action="store_true")

    sch = sub.add_parser("task", help="定时任务管理：查看 / 改 / 取消 / 清理历史")
    ssub = sch.add_subparsers(dest="task_cmd", required=True)

    sp = ssub.add_parser("list", help="列出任务（含周期、已跑场次、距今多久）")
    sp.add_argument("--session", default=None)
    sp.add_argument("--status", default=None,
                    choices=["pending", "all", "fired", "cancelled", "expired", "failed"],
                    help="默认只列待触发的；all 连历史一起看")
    sp.add_argument("--q", default=None, help="指令或人名里的关键词")
    sp.add_argument("--limit", type=int, default=50)

    sp = ssub.add_parser("show", help="看一个任务的全部字段")
    sp.add_argument("task_id", type=int)

    sp = ssub.add_parser("cancel", help="取消待触发的任务（周期项整条停下）")
    sp.add_argument("task_id", type=int)
    sp.add_argument("--yes", action="store_true")

    sp = ssub.add_parser("delete", help="物理删除一条记录（不代替取消）")
    sp.add_argument("task_id", type=int)
    sp.add_argument("--yes", action="store_true")

    sp = ssub.add_parser("purge", help="批量清历史：按状态 / 会话 / 多少天前（待触发的不算）")
    sp.add_argument("--status", default=None,
                    choices=["fired", "cancelled", "expired", "failed"])
    sp.add_argument("--session", default=None)
    sp.add_argument("--before-days", type=int, default=None, dest="before_days",
                    help="只删到期时间在这之前的")
    sp.add_argument("--yes", action="store_true")

    sp = ssub.add_parser("sweep", help="按保留天数与总行数上限清理历史")
    sp.add_argument("--yes", action="store_true")

    return p


def _llm_for_cli(cfg: Config):
    """写入记忆的取代判定要用 LLM；配置不全时退回直写（看记忆、删记忆这些不需要它）。"""
    try:
        from ggbot.llm.llm import LLMClient

        return LLMClient(cfg)
    except Exception as e:  # noqa: BLE001
        print(f"（{e}；写入记忆将不做取代判定）")
        return None


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
            if args.mem_cmd == "delete":
                cmd_memory_delete(storage, args.memory_id, args.yes)
                return
            if args.mem_cmd == "purge":
                cmd_memory_purge(storage, args.subject, args.kind, sid, args.yes)
                return
            memory = MemoryStore(cfg, storage, _llm_for_cli(cfg))
            if args.mem_cmd == "list":
                cmd_memory_list(storage, memory, sid, args.limit, args.subject, args.kind)
            elif args.mem_cmd == "search":
                cmd_memory_search(memory, args.query, args.k, sid)
            elif args.mem_cmd == "add":
                cmd_memory_add(memory, args.content, args.kind, args.subject,
                               args.importance, sid, args.expires_days)
            elif args.mem_cmd == "edit":
                cmd_memory_edit(memory, args.memory_id, args.content, args.importance,
                                args.subject, args.kind, args.expires)
            elif args.mem_cmd == "sweep":
                cmd_memory_sweep(memory, args.threshold, args.yes)
        elif args.cmd == "task":
            sid = _resolve_session(storage, args.session) if getattr(args, "session", None) else None
            if args.task_cmd == "show":
                cmd_task_show(storage, args.task_id)
                return
            if args.task_cmd == "cancel":
                cmd_task_cancel(storage, args.task_id, args.yes)
                return
            if args.task_cmd == "delete":
                cmd_task_delete(storage, args.task_id, args.yes)
                return
            if args.task_cmd == "purge":
                cmd_task_purge(storage, args.status, sid, args.before_days, args.yes)
                return
            tasks = _tasks_for_cli(cfg, storage)
            if args.task_cmd == "sweep":
                cmd_task_sweep(tasks, args.yes)
            else:
                st = "pending" if args.status is None else (
                    None if args.status == "all" else args.status)
                cmd_task_list(storage, sid, st, min(max(args.limit, 1), 500), args.q)
    finally:
        storage.close()


if __name__ == "__main__":
    main()
