"""定时提醒：到点把话发回会话，不经模型、不占额外一次请求。

行落在 SQLite 的 reminders 表（见 ggbot/core/storage.py），所以重启不丢：
后台巡检（Reminders.run）每 scan_interval_sec 扫一次表，到期就发。
- 过期宽限：进程停机期间错过的提醒，超过 miss_grace_sec 就置 expired 不再发——
  一小时后才冒出来的「五分钟后记得开会」只会让人莫名其妙。
- 发送走 presence.speak 这条回合外通道：先试还能用的被动回复锚点，回满次数才主动发；
  被本地频控拦下就留着下一轮重试，超宽限自然转 expired。
- 到点发的是当初写好的原文（不再问一次模型）：既省一次请求，也保证提醒的就是那句话。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta

from ..base import Tool
from ggbot.util.logging_setup import clip
from ggbot.util.session import oid8, session_label

logger = logging.getLogger("ggbot.tools.remind")

DATA_KEY = "schedule"
_WEEKDAYS = "一二三四五六日"

DEFAULT_CONFIG = {
    "enabled": True,
    "scan_interval_sec": 15,   # 巡检间隔，也就是提醒的触发精度
    "miss_grace_sec": 300,     # 过期宽限：错过这么久就不再发
    "max_pending": 20,         # 每个会话同时在飞的提醒条数
    "max_days": 30,            # 最远只能约到多少天后
    "max_text_chars": 200,
}

_UNITS = {"m": 60, "h": 3600, "d": 86400}
_REL_RE = re.compile(r"^\+(\d{1,5})([mhd])$")
_HM_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")
_MD_RE = re.compile(r"^(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?$")
_YMD_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?$")

WHEN_HELP = (
    "when 只认这几种写法：+30m / +2h / +1d（从现在起多久）、18:30（今天或明天的这个点）、"
    "10-30 18:30（今年）、2026-10-30 18:30"
)


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse_when(raw) -> tuple[int, str]:
    """把 when 换成 unix 秒；换不出来返回 (0, 错误说明)。只认固定写法，自然语言由模型换算。"""
    now = datetime.now().astimezone()
    s = str(raw or "").strip().replace("：", ":")
    if not s:
        return 0, f"when 不能为空。{WHEN_HELP}"

    m = _REL_RE.match(s)
    if m:
        return int(now.timestamp()) + int(m.group(1)) * _UNITS[m.group(2)], ""

    m = _HM_RE.match(s)
    if m:
        try:
            due = now.replace(hour=int(m.group(1)), minute=int(m.group(2)),
                               second=int(m.group(3) or 0), microsecond=0)
        except ValueError:
            return 0, f"时间不合法：{s}。{WHEN_HELP}"
        if due <= now:
            due += timedelta(days=1)  # 只说「18:30」而今天已经过了 → 明天这个点
        return int(due.timestamp()), ""

    m = _MD_RE.match(s)
    if m:
        due = _build(now.year, m.group(1), m.group(2), m.group(3), m.group(4), m.group(5))
        if due is None:
            return 0, f"日期不合法：{s}。{WHEN_HELP}"
        if due <= now:
            due = _build(now.year + 1, m.group(1), m.group(2), m.group(3), m.group(4), m.group(5))
            if due is None:
                return 0, f"日期不合法：{s}。{WHEN_HELP}"
        return int(due.timestamp()), ""

    m = _YMD_RE.match(s)
    if m:
        due = _build(int(m.group(1)), m.group(2), m.group(3), m.group(4), m.group(5), m.group(6))
        if due is None:
            return 0, f"日期不合法：{s}。{WHEN_HELP}"
        return int(due.timestamp()), ""

    return 0, f"看不懂这个时间：{s}。{WHEN_HELP}"


def _build(year, month, day, hour, minute, second) -> datetime | None:
    """按当前时区拼一个时间点，越界（13 月、2 月 30 日）返回 None。"""
    try:
        return datetime.now().astimezone().replace(
            year=int(year), month=int(month), day=int(day),
            hour=int(hour), minute=int(minute), second=int(second or 0), microsecond=0,
        )
    except ValueError:
        return None


def _fmt_due(ts: int) -> str:
    dt = datetime.fromtimestamp(int(ts))
    return f"{dt:%Y-%m-%d %H:%M} 星期{_WEEKDAYS[dt.weekday()]}"


def _span_text(sec: int) -> str:
    """把一段时间说成人话：35分 / 2小时5分 / 3天（天以下的精度到分）。"""
    sec = max(0, int(sec))
    days, rem = divmod(sec, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = []
    if days:
        parts.append(f"{days}天")
    if hours:
        parts.append(f"{hours}小时")
    if minutes and not days:
        parts.append(f"{minutes}分")
    return "".join(parts) or "不到一分钟"


class Reminders:
    """reminders 表的操作面：工具用它增删查，后台巡检用它触发。

    工具实例与巡检循环各持有一个 Reminders——所有状态都在表里，两份对象看到的是同一张表。
    speak（presence 提供的回合外发言）只有巡检循环用得上，工具侧留空即可。
    """

    def __init__(self, rt, speak=None):
        self.rt = rt
        self.speak = speak

    @property
    def config(self) -> dict:
        return self.rt.load_config(DATA_KEY, DEFAULT_CONFIG)

    @staticmethod
    def _who_label(row: dict) -> str:
        name = (row.get("notify_name") or "").strip()
        oid = oid8(row.get("notify") or "")
        return f"{name}/{oid}" if name and oid else (name or oid or "unknown")

    # ---------- 增删查（工具侧） ----------

    async def add(self, args: dict, ctx: dict) -> dict:
        cfg = self.config
        sid = str(ctx.get("session_id") or "").strip()
        if not sid:
            return {"ok": False, "error": "缺少会话标识，无法设提醒"}
        text = " ".join(str(args.get("what") or "").split())
        if not text:
            return {"ok": False, "error": "what 不能为空：到点要发出去的那句话"}
        char_cap = max(10, int(cfg.get("max_text_chars", 200) or 200))
        if len(text) > char_cap:
            return {"ok": False, "error": f"提醒内容太长（最多 {char_cap} 字），缩成一句话"}
        ts, err = parse_when(args.get("when"))
        if err:
            return {"ok": False, "error": err}
        now = int(time.time())
        if ts <= now:
            return {"ok": False, "error": "这个时间已经过去了，重新给一个将来的时间"}
        max_days = max(1, int(cfg.get("max_days", 30) or 30))
        if ts > now + max_days * 86400:
            return {"ok": False, "error": f"最远只能约 {max_days} 天内的提醒"}

        actor = ctx.get("actor") or {}
        owner = str(actor.get("openid") or "")
        name = str(args.get("who") or "").strip().lstrip("@")
        notify, notify_name = owner, (actor.get("name") or "")
        if name:
            oid = await asyncio.to_thread(self.rt.storage.find_sender_openid, name, sid)
            if not oid:
                return {"ok": False, "error": f"这个会话里找不到「{name}」；"
                                              "提醒本人不用填 who"}
            notify, notify_name = oid, name

        pending = await asyncio.to_thread(self.rt.storage.count_reminders, sid)
        max_pending = max(1, int(cfg.get("max_pending", 20) or 20))
        if pending >= max_pending:
            return {"ok": False, "error": f"这个会话已挂着 {pending} 条提醒，先用 remind_cancel 取消一些"}

        rid = await asyncio.to_thread(
            self.rt.storage.add_reminder, sid, owner, actor.get("name") or "",
            notify, notify_name, ts, text,
        )
        return {
            "ok": True,
            "id": rid,
            "due": _fmt_due(ts),
            "in": _span_text(ts - now) + "后",
            "who": notify_name or "本人",
            "text": text,
            "note": f"已排上 #{rid}，到点把这句话原样发回本会话（不会再问一次模型）",
        }

    async def list_rows(self, args: dict, ctx: dict) -> dict:
        sid = str(ctx.get("session_id") or "").strip()
        status = str(args.get("status") or "").strip().lower()
        if status in ("all", "全部", "历史"):
            status = None
        elif status and status != "pending":
            status = "pending"
        limit = max(1, min(50, int(args.get("limit", 20) or 20)))
        rows = await asyncio.to_thread(self.rt.storage.list_reminders, sid, status, limit)
        now = int(time.time())
        items = []
        for r in rows:
            gap = int(r["due_ts"]) - now
            items.append({
                "id": r["id"],
                "due": _fmt_due(r["due_ts"]),
                "when": f"{_span_text(abs(gap))}{'后' if gap >= 0 else '前'}",
                "text": r["text"],
                "who": r["notify_name"] or "本人",
                "status": r["status"],
            })
        return {"ok": True, "total": len(items), "items": items,
                "note": "编号用来 remind_cancel；只列本会话"}

    async def cancel(self, args: dict, ctx: dict) -> dict:
        ref = str(args.get("id") or "").strip()
        sid = str(ctx.get("session_id") or "").strip()
        if not ref:
            return {"ok": False, "error": "id 不能为空（remind_list 里的编号，或一句只对上唯一一条的关键词）"}
        rows = await asyncio.to_thread(self.rt.storage.list_reminders, sid, "pending", 50)
        target = None
        if ref.isdigit():
            target = next((r for r in rows if int(r["id"]) == int(ref)), None)
            if target is None:
                one = await asyncio.to_thread(self.rt.storage.get_reminder, int(ref))
                gone = f"（当前状态 {one['status']}）" if one else ""
                return {"ok": False, "error": f"#{ref} 不是本会话待触发的提醒{gone}"}
        else:
            hits = [r for r in rows if ref in (r["text"] or "")]
            if not hits:
                return {"ok": False, "error": f"内容里没有「{ref}」的待触发提醒，先 remind_list 看编号"}
            if len(hits) > 1:
                ids = "、".join(f"#{r['id']}（{clip(r['text'], 20)}）" for r in hits[:5])
                return {"ok": False, "error": f"「{ref}」对上 {len(hits)} 条，说清编号：{ids}"}
            target = hits[0]
        actor = ctx.get("actor") or {}
        oid = str(actor.get("openid") or "")
        if oid not in (target["owner"], target["notify"]) and int(actor.get("level") or 0) < 5:
            return {"ok": False, "error": "只能取消自己设的、或提醒自己的那条"}
        await asyncio.to_thread(self.rt.storage.finish_reminder, target["id"], "cancelled", _now_text())
        return {"ok": True, "cancelled": target["id"], "due": _fmt_due(target["due_ts"]),
                "text": target["text"], "note": "已取消，到点不会再发"}

    # ---------- 触发（巡检循环） ----------

    def _compose(self, row: dict) -> str:
        """群里提醒第三人时带上 @昵称，outbound 会换成真正的 @；私聊不用。"""
        text = row["text"]
        name = (row.get("notify_name") or "").strip()
        if str(row.get("session_id") or "").startswith("group:") and name \
                and f"@{name}" not in text:
            return f"@{name} {text}"
        return text

    async def tick(self) -> int:
        """扫一遍到期的提醒，返回这一轮发出去的条数。"""
        grace = max(0, int(self.config.get("miss_grace_sec", 300) or 0))
        now = int(time.time())
        fired = 0
        for row in await asyncio.to_thread(self.rt.storage.due_reminders, now):
            extra = {"session": session_label(row["session_id"])}
            late = now - int(row["due_ts"])
            if late > grace:
                await asyncio.to_thread(self.rt.storage.finish_reminder, row["id"], "expired")
                logger.warning("提醒过期未发（错过 %ds，宽限 %ds）#%d: %s",
                               late, grace, row["id"], clip(row["text"], 40), extra=extra)
                continue
            if self.speak is None:
                return fired  # 工具侧的实例不触发，交给巡检循环
            if not await self.speak(row["session_id"], self._compose(row)):
                continue  # 频控拦住或发送失败：下一轮再试，超宽限自然转 expired
            await asyncio.to_thread(self.rt.storage.finish_reminder,
                                    row["id"], "fired", _now_text())
            fired += 1
            logger.info("提醒到期触发 #%d（提醒 %s）", row["id"], self._who_label(row), extra=extra)
        return fired

    async def run(self) -> None:
        """后台巡检循环：间隔配在 scan_interval_sec，扫表即发，停机期间错过的按宽限处置。"""
        while True:
            await asyncio.sleep(max(2.0, float(self.config.get("scan_interval_sec", 15) or 15)))
            try:
                await self.tick()
            except Exception as e:  # noqa: BLE001 - 巡检不能因一次失败就停
                logger.warning("提醒巡检失败: %s", clip(str(e), 80))


class _RemindTool(Tool):
    data_name = DATA_KEY
    default_config = DEFAULT_CONFIG

    def __init__(self, rt, rem: Reminders):
        super().__init__(rt)
        self.rem = rem


class RemindAdd(_RemindTool):
    name = "remind_add"
    min_level = 2
    description = (
        "定时提醒：到点把 what 这句话原样发回当前会话（不会再问一次模型），"
        "用来兑现「过一会儿提醒你」「明天 8 点叫我看结果」这类约定。"
        f"when 只认固定写法，自然语言（明天下午三点）先调 time 拿到今天日期再换算成绝对时间：{WHEN_HELP}。"
        "what 写成到点该说的那句人话，要让人看明白是什么事（「该去交材料了」，不是「提醒一下」）。"
        "默认提醒说话的人本人；对方明确让你提醒别人才填 who（只认这个会话里出现过的昵称）。"
        "设好后在正文里回一句约好的时间，别设完不作声"
    )
    parameters = {
        "type": "object",
        "properties": {
            "when": {"type": "string", "description": "什么时候提醒：+30m / +2h / +1d，或 18:30 / 10-30 18:30 / 2026-10-30 18:30"},
            "what": {"type": "string", "description": "到点发出去的那句话，一句话说清是什么事"},
            "who": {"type": "string", "description": "可选：提醒别人的昵称，默认提醒本人"},
        },
        "required": ["when", "what"],
    }

    async def run(self, args: dict, ctx: dict) -> dict:
        return await self.rem.add(args, ctx)


class RemindList(_RemindTool):
    name = "remind_list"
    config_owner = False
    toggleable = False
    description = (
        "看本会话挂着哪些提醒（默认只看还没触发的，按到期时间排序）。"
        "对方问「我让你提醒我什么来着」「还有几个提醒」时调用，照返回的编号和时间说"
    )
    parameters = {
        "type": "object",
        "properties": {
            "status": {"type": "string", "description": "可选：all 连已触发/已取消的历史一起看，默认只看待触发的"},
            "limit": {"type": "integer", "description": "返回条数，默认 20，最多 50"},
        },
    }

    async def run(self, args: dict, ctx: dict) -> dict:
        return await self.rem.list_rows(args, ctx)


class RemindCancel(_RemindTool):
    name = "remind_cancel"
    config_owner = False
    toggleable = False
    description = (
        "取消一条还没触发的提醒。id 填 remind_list 里的编号；"
        "内容只对上唯一一条时也可以直接给那句关键词。"
        "只能取消自己设的、或提醒自己的那条；已经发出去的取消不了"
    )
    parameters = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "提醒编号，或一句只对上唯一一条的关键词"},
        },
        "required": ["id"],
    }

    async def run(self, args: dict, ctx: dict) -> dict:
        return await self.rem.cancel(args, ctx)


def get_tools(rt):
    rem = Reminders(rt)
    return [RemindAdd(rt, rem), RemindList(rt, rem), RemindCancel(rt, rem)]
