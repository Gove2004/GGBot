"""面板的读数层：GET /api/page/<name>，一进页面一次拿齐。

面板的读全部收在这一条接口上：一次往返就是这一页要画的完整快照，筛选/分页参数原样带在 query 上，
口径校验留在各页自己的 loader 里。写操作不跟着动——仍然按资源走（resources / settings），
改完由前端重新拉一次这一页。
一份数据一个名字（sessions / memories / tasks…），多页共用的那份只有一份实现。

一页要读哪几份数据全在 PAGES 这张表里；前端 PAGE_META 有同名条目才拉得到数据。
"""

from __future__ import annotations

import asyncio
import re
import time
from urllib.parse import quote

from fastapi import HTTPException, Request

from ggbot.core.auth import LEVEL_LOCKED, LEVEL_LOCKED_NAME, LEVEL_MAX, LEVEL_NAMES
from ggbot.core.config import ALLOWED_SETTINGS, RESTART_SETTINGS
from ggbot.llm.memory import IMPORTANCE_MAX, IMPORTANCE_MIN, KIND_LABELS
from ggbot.util.session import SYSTEM_LABEL, session_label
from ggbot.web.guards import mask_secrets, panel_of, protected_router
from ggbot.web.settings import system_prompt_payload

# 四列日志：时间 | 等级 | 会话 | 内容（等级/会话按宽度右填充空格）。
# 只认这一份格式：日志是追加写的，但对不上的行（堆栈续行）本来就走原样保留的兜底，
# 旧的三列/五列格式不需要再对冲——那几版只活了几小时就被换掉，线上日志里没有它们。
LOG_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) \| (\w+)\s*\| ((?:群聊|私聊|系统)[0-9A-Za-z]{8})\s*\| (.*)$"
)

# 页面读数的固定量：会话清单一次给全、仪表盘最近消息 8 条、
# 日志页一次取最近 400 行（前端不再自己报数，面板要的窗口大小只在这一定义）
SESSIONS_LIMIT = 500
RECENT_LIMIT = 8
LOG_LINES_DEFAULT = 400
LOG_LINES_MAX = 2000

router = protected_router()


def level_contract() -> dict:
    """等级这本契约：级别名字表与上下限。面板要显示「L几 什么」都从这里取，前端不留第二份。"""
    return {"level_names": {str(k): v for k, v in LEVEL_NAMES.items()},
            "max_level": LEVEL_MAX, "locked_level": LEVEL_LOCKED,
            "locked_name": LEVEL_LOCKED_NAME}


def _day_start_ts(now_ts: int) -> int:
    """服务器本地「今天零点」的 unix 秒（面板算「今天要发几条」按这个切）。"""
    lt = time.localtime(now_ts)
    return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))


class _Q:
    """query 参数的取值口径：缺省、非法、越界都在这层收掉，loader 里只留业务。"""

    def __init__(self, request: Request):
        self._d = request.query_params

    def text(self, key: str) -> str:
        return (self._d.get(key) or "").strip()

    def num(self, key: str, default: int, lo: int | None = None, hi: int | None = None) -> int:
        raw = self._d.get(key)
        try:
            n = default if raw in (None, "") else int(raw)
        except ValueError:
            n = default
        if lo is not None:
            n = max(lo, n)
        if hi is not None:
            n = min(hi, n)
        return n


# 会话清单与最近消息不带参数：面板要的永远是「全部会话 + 最近 8 条」，
# 参数留着只会让「记忆页的 limit」和「会话清单的 limit」撞名。
async def _ds_sessions(p, q: _Q) -> dict:
    return {"sessions": await asyncio.to_thread(_live_sessions, p, SESSIONS_LIMIT)}


def _live_sessions(p, limit: int) -> list[dict]:
    rows = p.storage.list_sessions(limit)
    live = {s["sid"]: s for s in (p.presence.status().get("sessions") or [])}
    for row in rows:
        row["label"] = session_label(row.get("id"))
        st = live.get(row.get("id"))
        row["live"] = st or {"state": "offline", "reason": "idle",
                             "remaining_sec": 0, "idle_sec": None}
    return rows


async def _ds_recent(p, q: _Q) -> dict:
    return {"recent": await asyncio.to_thread(p.storage.recent_messages_global, RECENT_LIMIT)}


async def _ds_logs(p, q: _Q) -> dict:
    from ggbot.util.logging_setup import tail_log

    level = q.text("level") or "DEBUG"
    session, needle = q.text("session"), q.text("q")
    n = q.num("lines", LOG_LINES_DEFAULT, 1, LOG_LINES_MAX)
    # 会话/内容筛选要先筛后截：先取最近 n 行再筛，等于只能在这么宽的窗口里搜，
    # 窗口外的日志永远筛不出来（等级门槛在 tail_log 里已经先筛过）
    raw = tail_log(p.cfg.log_file, None if (session or needle) else n, level)
    ses, want = session.lower(), needle.lower()
    logs = []
    for line in raw:
        m = LOG_LINE_RE.match(line)
        if m:
            ts, lv, sess, content = m.groups()
        else:
            # 认不出的行 = 异常堆栈的续行（四列格式是现在唯一的写法）。原样归到
            # 「系统」保留，别因为解析失败就丢整行，否则报错只剩第一句
            ts, lv, sess, content = "", "-", SYSTEM_LABEL, line
        if ses and ses not in sess.lower():
            continue
        if want and want not in content.lower():
            continue
        logs.append({"ts": ts[:23], "level": lv, "session": sess, "content": content})
    total = len(logs)
    try:
        log_name = p.cfg.log_file.relative_to(
            p.cfg.log_file.parent.parent.parent).as_posix()
    except ValueError:
        log_name = p.cfg.log_file.name
    return {"logs": {"level": level, "file": log_name, "logs": logs[-n:], "total": total}}


async def _ds_tasks(p, q: _Q) -> dict:
    from tool.task._rule import rule_label as _rule_label

    st = q.text("status") or "pending"
    st = None if st == "all" else st
    sid, needle = q.text("session_id") or None, q.text("q") or None
    offset = q.num("offset", 0, 0)
    rows = await asyncio.to_thread(
        p.storage.list_tasks, sid, st, q.num("limit", 200, 1, 500), offset, needle)
    total = p.storage.count_tasks(sid, st, needle)
    now = int(time.time())
    stats = await asyncio.to_thread(p.storage.task_stats, now, _day_start_ts(now))
    for r in rows:
        r["rule_label"] = _rule_label(r.get("rule") or "")
    tasks = p.tasks_or_none()
    tool_cfg = tasks.config if tasks else {}
    return {"tasks": {
        "items": rows, "total": total, "offset": offset, "stats": stats,
        "limits": {k: tool_cfg.get(k) for k in
                   ("max_pending", "max_days", "max_instruction_chars",
                    "min_interval_sec", "max_failures", "keep_history_days",
                    "max_rows", "miss_grace_sec")}}}


async def _ds_memories(p, q: _Q) -> dict:
    sid, subject, kind = q.text("session_id") or None, q.text("subject") or None, q.text("kind") or None
    needle = q.text("q")
    mcfg = p.memory.m
    out = {
        "limits": {"max_memories": mcfg["max_memories"],
                   "max_per_subject": mcfg["max_per_subject"],
                   "forget_after_days": mcfg["forget_after_days"]},
        # 类别的取值与叫法、重要度的区间是记忆这一层的契约：
        # 面板的选项、徽章和 ★ 数都从这份生成，前端不留第二份手写数
        "kinds": KIND_LABELS,
        "importance_min": IMPORTANCE_MIN,
        "importance_max": IMPORTANCE_MAX,
        # 整库容量读数与筛选无关，而且已经按遗忘扫描的口径扣掉到期/淡出
        "usage": await asyncio.to_thread(p.memory.usage),
        "searched": bool(needle),
    }
    if needle:
        # 「检索」也只列命中，但 limits/usage 照样带上——临界提示在检索时说的是同一套口径
        hits = await asyncio.to_thread(
            p.memory.search, needle, q.num("k", 50, 1, 50), sid, None)
        out["items"] = await asyncio.to_thread(p.memory.panel_rows, hits)
        out["total"] = len(out["items"])
    else:
        rows = await asyncio.to_thread(
            p.storage.list_memories, sid, q.num("limit", 200, 1, 500), q.num("offset", 0, 0),
            subject, kind)
        out["items"] = await asyncio.to_thread(p.memory.panel_rows, rows)
        out["total"] = p.storage.count_memories(sid, subject, kind)
    return {"memories": out}


async def _ds_emojis(p, q: _Q) -> dict:
    lib = p.emoji_lib()
    if lib is None:
        return {"emojis": {"items": [], "mode": "none", "max_stickers": 0, "max_image_bytes": 0}}
    # force=1：面板「刷新」按钮要立刻看到刚丢进目录的图；平时用扫目录缓存
    rows = await lib.rows(force=bool(q.num("force", 0, 0, 1)))
    return {"emojis": {
        "mode": lib.matcher.mode,
        "max_stickers": int(lib.config.get("max_stickers", 0) or 0),
        "max_image_bytes": lib.max_image_bytes,
        "items": [
            {
                "file": r["file"],
                "desc": r["desc"],
                "added_at": r["added_at"],
                "use_count": r["use_count"],
                "last_used_at": r["last_used_at"],
                "has_vec": r["vec"] is not None,
                "url": f"/emoji/{quote(r['file'])}",
            }
            for r in rows
        ],
    }}


async def _ds_tools(p, q: _Q) -> dict:
    ex = p.executor
    rows = [] if ex is None else ex.list_tools()
    for row in rows:
        # 密钥只在这里脱敏：工具配置既给「工具」页也给「权限」页，两份都得是脱敏后的
        row["config"] = mask_secrets(row.get("config") or {})
    # 门槛要显示成「L4 心腹」，级别名字表得跟着这一页来——工具页可能先于权限页打开，
    # 权限页也共用这份读数，两处不必各写一遍
    return {"tools": rows, "levels": level_contract()}


async def _ds_system_prompt(p, q: _Q) -> dict:
    return {"system_prompt": system_prompt_payload(p)}


async def _ds_settings(p, q: _Q) -> dict:
    return {"settings": {"config": mask_secrets(p.cfg.data), "allowed": ALLOWED_SETTINGS,
                         "restart": RESTART_SETTINGS}}


async def _ds_permissions(p, q: _Q) -> dict:
    perms = p.perms()
    return {"permissions": {"members": perms.members(), "default_level": perms.default_level}}


PAGES: dict[str, list] = {
    "dash": [_ds_sessions, _ds_recent],
    "logs": [_ds_logs],
    "sessions": [_ds_sessions],
    "task": [_ds_sessions, _ds_tasks],
    "memory": [_ds_sessions, _ds_memories],
    "emojis": [_ds_emojis],
    "persona": [_ds_system_prompt],
    "settings": [_ds_settings],
    "permissions": [_ds_permissions, _ds_tools],
    "tools": [_ds_tools],
}


@router.get("/api/page/{name}")
async def page_data(name: str, request: Request):
    loaders = PAGES.get(name)
    if loaders is None:
        raise HTTPException(status_code=404, detail="没有这一页")
    p = panel_of(request)
    q = _Q(request)
    out: dict = {}
    for load in loaders:
        out.update(await load(p, q))
    return out
