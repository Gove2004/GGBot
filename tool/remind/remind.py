"""日程：到点把话发回会话，单次和周期都靠同一条巡检循环兑现。

行落在 SQLite 的 reminders 表（见 ggbot/core/storage.py），所以重启不丢：
后台巡检（Reminders.run）每 scan_interval_sec 扫一次表，到期就发。
- 周期项（daily / weekdays / weekly / monthly）发完一场就把 due_ts 推到下一场，
  状态始终是 pending；排满 times 次或规则算不出下一场才转 fired 退场。
- 过期宽限：进程停机期间错过的提醒，超过 miss_grace_sec 就不补发——
  一小时后才冒出来的「五分钟后记得开会」只会让人莫名其妙。周期项错过的是**那一两场**，
  不整条作废：跳过没赶上的场次，直接推到下一场继续排。
- 发送走 presence.speak 这条回合外通道：先试还能用的被动回复锚点，回满次数才主动发；
  被本地频控拦下就留着下一轮重试，超宽限自然转 expired。
- 到点发的是当初写好的原文（不再问一次模型）：既省一次请求，也保证提醒的就是那句话。
- 历史（已触发/已取消/已过期）按保留天数与总行数上限退场，pending 永不参与清理。
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta

from ..base import Tool
from ggbot.util.logging_setup import clip
from ggbot.util.session import oid8, session_label
from ._rule import (REPEAT_HELP, WHEN_HELP, advance, fmt_due, parse_rule, parse_when,
                    rule_label, span_text)

logger = logging.getLogger("ggbot.tools.remind")

DATA_KEY = "remind"

DEFAULT_CONFIG = {
    "enabled": True,
    "scan_interval_sec": 15,      # 巡检间隔，也就是提醒的触发精度
    "miss_grace_sec": 300,        # 过期宽限：错过这么久就不发这一场
    "max_pending": 20,            # 每个会话同时在飞的日程条数
    "max_days": 30,              # 第一次触发最远只能约到多少天后
    "max_text_chars": 200,
    "max_repeat_times": 100,      # 周期项最多排几场（times=0 表示不限，靠取消退场）
    "keep_history_days": 30,      # 已触发/已取消/已过期的行留多少天，过了就删
    "max_rows": 1000,             # 表里总行数上限，超了从最旧的历史行删起（不动 pending）
    "sweep_interval_sec": 21600,  # 巡检循环顺带清理历史的间隔
}

_REPEAT_MAX_SCAN = 5000  # 错过太多场时补算下一场的循环上限，防止长期停机后卡在扫描里


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Reminders:
    """reminders 表的操作面：工具用它增删查，后台巡检用它触发与清理。

    工具实例与巡检循环各持有一个 Reminders——所有状态都在表里，两份对象看到的是同一张表。
    speak（presence 提供的回合外发言）只有巡检循环用得上，工具侧留空即可。
    """

    def __init__(self, rt, speak=None):
        self.rt = rt
        self.speak = speak
        self._sweep_at = 0.0

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
            return {"ok": False, "error": f"最远只能约 {max_days} 天内的第一次触发"}
        rule, err = parse_rule(args.get("repeat"), ts)
        if err:
            return {"ok": False, "error": err}

        times = 1
        if rule:
            raw_times = args.get("times")
            try:
                times = 0 if raw_times in (None, "") else int(raw_times)
            except (TypeError, ValueError):
                return {"ok": False, "error": "times 得是个整数（排几场），0 表示一直排到被取消"}
            if times < 0:
                return {"ok": False, "error": "times 不能是负数；不想限次数就填 0"}
            cap = max(1, int(cfg.get("max_repeat_times", 100) or 100))
            if times > cap:
                return {"ok": False, "error": f"一次最多排 {cap} 场；想长期就用 times=0 并记得取消"}

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
            return {"ok": False, "error": f"这个会话已挂着 {pending} 条提醒，先用 remind_del 取消一些"}

        rid = await asyncio.to_thread(
            self.rt.storage.add_reminder, sid, owner, actor.get("name") or "",
            notify, notify_name, ts, text, rule, times,
        )
        out = {
            "ok": True,
            "id": rid,
            "due": fmt_due(ts),
            "in": span_text(ts - now) + "后",
            "who": notify_name or "本人",
            "text": text,
            "repeat": rule_label(rule),
            "times": times,
        }
        if rule:
            plan = f"排 {times} 场" if times > 0 else "一直排到被取消"
            out["note"] = (f"已排上 #{rid}（{rule_label(rule)}，{plan}）：到点把这句话原样发回本会话，"
                           "发完自动排下一场，不会再问一次模型")
        else:
            out["note"] = f"已排上 #{rid}，到点把这句话原样发回本会话（不会再问一次模型）"
        return out

    async def list_rows(self, args: dict, ctx: dict) -> dict:
        sid = str(ctx.get("session_id") or "").strip()
        status = str(args.get("status") or "").strip().lower()
        # 默认只列还在排的：填了别的词也当 pending，看历史要明说 all
        status = None if status in ("all", "全部", "历史") else "pending"
        limit = max(1, min(50, int(args.get("limit", 20) or 20)))
        rows = await asyncio.to_thread(self.rt.storage.list_reminders, sid, status, limit)
        now = int(time.time())
        items = []
        for r in rows:
            gap = int(r["due_ts"]) - now
            times = int(r.get("times") or 0)  # 0 是「不限场数」，不能当成缺省的 1 场
            fired = int(r.get("fired_count") or 0)
            items.append({
                "id": r["id"],
                "due": fmt_due(r["due_ts"]),
                "when": f"{span_text(abs(gap))}{'后' if gap >= 0 else '前'}",
                "text": r["text"],
                "who": r["notify_name"] or "本人",
                "status": r["status"],
                "repeat": rule_label(r.get("rule") or ""),
                "fires": f"{fired}/{times}" if r.get("rule") and times > 0 else (str(fired) if r.get("rule") else ""),
            })
        return {"ok": True, "total": len(items), "items": items,
                "note": "编号用来 remind_del（周期项一取消就是整条停下）；只列本会话"}

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
        out = {"ok": True, "cancelled": target["id"], "due": fmt_due(target["due_ts"]),
               "text": target["text"], "note": "已取消，到点不会再发"}
        if target.get("rule"):
            out["repeat"] = rule_label(target["rule"])
            out["note"] = (f"已取消整条{rule_label(target['rule'])}日程（#{target['id']}），"
                           f"已发过 {int(target.get('fired_count') or 0)} 场，后面不再发")
        return out

    # ---------- 触发（巡检循环） ----------

    def _compose(self, row: dict) -> str:
        """群里提醒第三人时带上 @昵称，outbound 会换成真正的 @；私聊不用。"""
        text = row["text"]
        name = (row.get("notify_name") or "").strip()
        if str(row.get("session_id") or "").startswith("group:") and name \
                and f"@{name}" not in text:
            return f"@{name} {text}"
        return text

    @staticmethod
    def _catch_up(due_ts: int, rule: str, now: int) -> tuple[int, int]:
        """错过的周期项：从原本那一格往后推到第一场还没到的那一格，返回（下一场, 跳过了几场）。"""
        nxt = int(due_ts)
        skipped = 0
        while nxt <= now and skipped < _REPEAT_MAX_SCAN:
            pushed = advance(nxt, rule)
            if not pushed:  # 规则算不出下一场（不该发生），按跑完处理
                break
            nxt, skipped = pushed, skipped + 1
        return nxt, skipped

    async def tick(self) -> int:
        """扫一遍到期的日程，返回这一轮真正发出去的条数。"""
        if self.speak is None:
            return 0  # 工具侧的实例不触发，交给巡检循环
        cfg = self.config
        grace = max(0, int(cfg.get("miss_grace_sec", 300) or 0))
        now = int(time.time())
        fired = 0
        for row in await asyncio.to_thread(self.rt.storage.due_reminders, now):
            extra = {"session": session_label(row["session_id"])}
            late = now - int(row["due_ts"])
            rule = row.get("rule") or ""
            if late > grace:
                if rule:
                    nxt, skipped = self._catch_up(int(row["due_ts"]), rule, now)
                    await asyncio.to_thread(self.rt.storage.skip_reminder, row["id"], nxt)
                    logger.warning("提醒跳过 %d 场未发（错过 %ds，宽限 %ds）#%d: %s",
                                   skipped, late, grace, row["id"], clip(row["text"], 40), extra=extra)
                    continue
                await asyncio.to_thread(self.rt.storage.finish_reminder, row["id"], "expired")
                logger.warning("提醒过期未发（错过 %ds，宽限 %ds）#%d: %s",
                               late, grace, row["id"], clip(row["text"], 40), extra=extra)
                continue
            if not await self.speak(row["session_id"], self._compose(row)):
                continue  # 频控拦住或发送失败：下一轮再试，超宽限自然转 expired
            nxt, done_times = await self._settle(row)
            fired += 1
            if nxt:
                logger.info("提醒到期触发 #%d 第 %d 场（%s，下一场 %s）",
                            row["id"], int(row.get("fired_count") or 0) + 1, rule_label(rule),
                            fmt_due(nxt), extra=extra)
            else:
                tail = "，已排满" if done_times else ""
                logger.info("提醒到期触发 #%d（提醒 %s%s）", row["id"], self._who_label(row),
                            tail, extra=extra)
        return fired

    async def _settle(self, row: dict) -> tuple[int, bool]:
        """一场发完：周期项记一次并把到期推到下一场，一次性项或排满场次的直接置 fired。

        返回（下一场的到期时间, 是不是因为排满场次才结束）；调用方只拿它写日志，
        状态已经在这一步落进表里，所以发送成功与落库不会分成两套口径。
        """
        now_text = _now_text()
        rule = row.get("rule") or ""
        if not rule:
            await asyncio.to_thread(self.rt.storage.finish_reminder, row["id"], "fired", now_text)
            return 0, False
        nxt = advance(int(row["due_ts"]), rule)
        times = int(row.get("times") or 0)
        done_times = times > 0 and int(row.get("fired_count") or 0) + 1 >= times
        if not nxt or done_times:
            # 这最后一场也发出去了，所以场数要记上，不能只置状态
            await asyncio.to_thread(self.rt.storage.complete_reminder, row["id"], now_text)
            return 0, done_times
        await asyncio.to_thread(self.rt.storage.reschedule_reminder, row["id"], nxt, now_text)
        return nxt, False

    # ---------- 清理（历史退场） ----------

    def sweep(self) -> dict:
        """终态行按保留天数退场，整表超上限再从最旧的历史行开始腾地方；pending 一律不动。"""
        cfg = self.config
        keep_days = max(1, int(cfg.get("keep_history_days", 30) or 30))
        max_rows = max(1, int(cfg.get("max_rows", 1000) or 1000))
        cutoff = (datetime.now() - timedelta(days=keep_days)).strftime("%Y-%m-%d %H:%M:%S")
        aged = self.rt.storage.delete_terminal_before(cutoff)
        # 上限比的是整表行数（含还在排的），不是待触发的条数——不然 max_rows 会跟着巡检抖动
        total = self.rt.storage.count_reminders(status=None)
        over = max(0, total - max_rows)
        culled = self.rt.storage.delete_oldest_terminal(over) if over else 0
        left = total - culled
        logger.info("日程清理 保留 %d 天 · 到期删 %d · 超限删 %d（共 %d 条）",
                    keep_days, aged, culled, left)
        return {"aged": aged, "over": culled, "total": left, "keep_days": keep_days}

    async def run(self) -> None:
        """后台巡检循环：间隔配在 scan_interval_sec，扫表即发，停机期间错过的按宽限处置。"""
        self._sweep_at = time.monotonic()
        while True:
            await asyncio.sleep(max(2.0, float(self.config.get("scan_interval_sec", 15) or 15)))
            try:
                await self.tick()
            except Exception as e:  # noqa: BLE001 - 巡检不能因一次失败就停
                logger.warning("日程巡检失败: %s", clip(str(e), 80))
            interval = max(60, int(self.config.get("sweep_interval_sec", 21600) or 21600))
            if time.monotonic() - self._sweep_at >= interval:
                self._sweep_at = time.monotonic()
                try:
                    await asyncio.to_thread(self.sweep)
                except Exception as e:  # noqa: BLE001
                    logger.warning("日程历史清理失败: %s", clip(str(e), 80))


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
        "用来兑现「过一会儿提醒你」「明天 8 点叫我看结果」「每天打卡」这类约定。"
        f"when 只认固定写法，自然语言（明天下午三点）先调 time 拿到今天日期再换算成绝对时间：{WHEN_HELP}。"
        f"要周期就填 repeat：{REPEAT_HELP}；repeat 填了才用 times 限制排几场（0=一直排到被取消）。"
        "what 写成到点该说的那句人话，要让人看明白是什么事（「该去交材料了」，不是「提醒一下」）。"
        "默认提醒说话的人本人；对方明确让你提醒别人才填 who（只认这个会话里出现过的昵称）。"
        "设好后在正文里回一句约好的时间（周期就说清多久一次），别设完不作声"
    )
    parameters = {
        "type": "object",
        "properties": {
            "when": {"type": "string", "description": "第一次什么时候提醒：+30m / +2h / +1d，或 18:30 / 10-30 18:30 / 2026-10-30 18:30"},
            "what": {"type": "string", "description": "到点发出去的那句话，一句话说清是什么事"},
            "repeat": {"type": "string", "description": "可选：daily / weekdays / weekly / monthly，不填就是一次性"},
            "times": {"type": "integer", "description": "可选（只有填了 repeat 才生效）：排几场，0 或不填=一直排到被取消"},
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
        "看本会话挂着哪些提醒（默认只看还没结束的，按下一次到期时间排序，"
        "周期项会带上「每天/每周/…」和已发几场）。"
        "对方问「我让你提醒我什么来着」「还有几个提醒」时调用，照返回的编号和时间说"
    )
    parameters = {
        "type": "object",
        "properties": {
            "status": {"type": "string", "description": "可选：all 连已结束/已取消/已过期的历史一起看，默认只看待触发的"},
            "limit": {"type": "integer", "description": "返回条数，默认 20，最多 50"},
        },
    }

    async def run(self, args: dict, ctx: dict) -> dict:
        return await self.rem.list_rows(args, ctx)


class RemindDel(_RemindTool):
    name = "remind_del"
    config_owner = False
    toggleable = False
    description = (
        "取消一条还没结束的提醒（置为已取消，记录仍保留）。id 填 remind_list 里的编号；"
        "内容只对上唯一一条时也可以直接给那句关键词。"
        "取消周期项等于取消整条日程（后面不再发，已发过的场次还在）。"
        "只能取消自己设的、或提醒自己的那条；已经发出去的一次性提醒取消不了"
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
    return [RemindAdd(rt, rem), RemindList(rt, rem), RemindDel(rt, rem)]
