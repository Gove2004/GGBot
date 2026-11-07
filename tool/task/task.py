"""定时任务：到点跑一个完整回合，单次和周期都靠同一条巡检循环兑现。

行落在 SQLite 的 tasks 表（见 ggbot/core/storage.py），所以重启不丢：
后台巡检（Tasks.run）每 scan_interval_sec 扫一次表，到期就把 instruction 交给模型跑一轮。
- 载荷是**指令**不是台词：到点走 presence.run_task → 上下文组装 → agent 循环，
  模型能 fetch/search 抓网页、kv_get/kv_set 存取上次的数据算增量，再决定要不要汇报。
- 一场跑完按状态落定：sent（发出去了）与 silence（模型判断没什么可说）都算跑过一场；
  blocked（频控拦住）留着下一轮重试；error / denied 记一次失败，连着失败 max_failures 次
  就置 failed 退场，不再占着 pending 反复烧请求。
- 周期项（daily / weekdays / weekly / monthly / every:30m）跑完一场就把 due_ts 推到下一场，
  状态始终是 pending；排满 times 次或规则算不出下一场才转 fired 退场。
- 过期宽限：进程停机期间错过的任务，超过 miss_grace_sec 就不补跑——
  一小时后才冒出来的「五分钟后记得开会」只会让人莫名其妙。周期项错过的是**那一两场**，
  不整条作废：跳过没赶上的场次，直接推到下一场继续排。
- 历史（已跑完/已取消/已过期/失败）按保留天数与总行数上限退场，pending 永不参与清理。
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
                    rule_label, rule_seconds, span_text)

logger = logging.getLogger("ggbot.tools.task")

DATA_KEY = "task"

DEFAULT_CONFIG = {
    "enabled": True,
    "scan_interval_sec": 15,      # 巡检间隔，也就是任务的触发精度
    "miss_grace_sec": 300,        # 过期宽限：错过这么久就不跑这一场
    "max_pending": 20,            # 每个会话同时在飞的任务条数
    "max_days": 30,               # 第一次触发最远只能约到多少天后
    "max_instruction_chars": 500,
    "min_interval_sec": 300,      # every: 规则的下限——每场都要跑一次模型，太密就是烧钱
    "max_failures": 5,            # 连着失败这么多次就置 failed 退场
    "max_repeat_times": 100,      # 周期项最多排几场（times=0 表示不限，靠取消退场）
    "keep_history_days": 30,      # 终态行留多少天，过了就删
    "max_rows": 1000,             # 表里总行数上限，超了从最旧的历史行删起（不动 pending）
    "sweep_interval_sec": 21600,  # 巡检循环顺带清理历史的间隔
}

_REPEAT_MAX_SCAN = 5000  # 错过太多场时补算下一场的循环上限，防止长期停机后卡在扫描里

# 失败原因写进 tasks.last_error（面板展示）；具体报错在日志里，带会话与任务编号
_FAIL_REASONS = {
    "error": "模型请求或工具报错（详情见日志）",
    "denied": "创建者权限已降为禁止",
}


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Tasks:
    """tasks 表的操作面：工具用它增删查，后台巡检用它触发与清理。

    工具实例与巡检循环各持有一个 Tasks——所有状态都在表里，两份对象看到的是同一张表。
    run_task（presence 提供的「跑一个完整回合」）只有巡检循环用得上，工具侧留空即可。
    """

    def __init__(self, rt, run_task=None):
        self.rt = rt
        self.run_task = run_task
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
            return {"ok": False, "error": "缺少会话标识，无法排任务"}
        instruction = str(args.get("instruction") or "").strip()
        if not instruction:
            return {"ok": False, "error": "instruction 不能为空：到点要去做的那件事"}
        char_cap = max(10, int(cfg.get("max_instruction_chars", 500) or 500))
        if len(instruction) > char_cap:
            return {"ok": False,
                    "error": f"指令太长（最多 {char_cap} 字），说清查什么、跟什么比、什么时候汇报就够"}
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
        gap = rule_seconds(rule)
        floor = max(60, int(cfg.get("min_interval_sec", 300) or 300))
        if gap and gap < floor:
            return {"ok": False,
                    "error": f"间隔太短：每场都要跑一次模型，最快 {span_text(floor)}一次"}

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
                                              "汇报给本人不用填 who"}
            notify, notify_name = oid, name

        pending = await asyncio.to_thread(self.rt.storage.count_tasks, sid)
        max_pending = max(1, int(cfg.get("max_pending", 20) or 20))
        if pending >= max_pending:
            return {"ok": False, "error": f"这个会话已挂着 {pending} 条任务，先用 task_del 取消一些"}

        rid = await asyncio.to_thread(
            self.rt.storage.add_task, sid, owner, actor.get("name") or "",
            notify, notify_name, ts, instruction, rule, times,
        )
        out = {
            "ok": True,
            "id": rid,
            "due": fmt_due(ts),
            "in": span_text(ts - now) + "后",
            "who": notify_name or "本人",
            "instruction": instruction,
            "repeat": rule_label(rule),
            "times": times,
        }
        if rule:
            plan = f"排 {times} 场" if times > 0 else "一直排到被取消"
            out["note"] = (f"已排上 #{rid}（{rule_label(rule)}，{plan}）：到点按这条指令跑一轮，"
                           "该查该比都会真去做，跑完自己决定要不要汇报")
        else:
            out["note"] = f"已排上 #{rid}，到点按这条指令跑一轮（该查该比都会真去做）"
        return out

    async def list_rows(self, args: dict, ctx: dict) -> dict:
        sid = str(ctx.get("session_id") or "").strip()
        status = str(args.get("status") or "").strip().lower()
        # 默认只列还在排的：填了别的词也当 pending，看历史要明说 all
        status = None if status in ("all", "全部", "历史") else "pending"
        limit = max(1, min(50, int(args.get("limit", 20) or 20)))
        rows = await asyncio.to_thread(self.rt.storage.list_tasks, sid, status, limit)
        now = int(time.time())
        items = []
        for r in rows:
            gap = int(r["due_ts"]) - now
            times = int(r.get("times") or 0)  # 0 是「不限场数」，不能当成缺省的 1 场
            fired = int(r.get("fired_count") or 0)
            item = {
                "id": r["id"],
                "due": fmt_due(r["due_ts"]),
                "when": f"{span_text(abs(gap))}{'后' if gap >= 0 else '前'}",
                "instruction": r["instruction"],
                "who": r["notify_name"] or "本人",
                "status": r["status"],
                "repeat": rule_label(r.get("rule") or ""),
                "runs": f"{fired}/{times}" if r.get("rule") and times > 0 else (str(fired) if r.get("rule") else ""),
            }
            fails = int(r.get("fail_count") or 0)
            if fails:
                item["fails"] = fails
                item["last_error"] = r.get("last_error") or ""
            items.append(item)
        return {"ok": True, "total": len(items), "items": items,
                "note": "编号用来 task_del（周期项一取消就是整条停下）；只列本会话"}

    async def cancel(self, args: dict, ctx: dict) -> dict:
        ref = str(args.get("id") or "").strip()
        sid = str(ctx.get("session_id") or "").strip()
        if not ref:
            return {"ok": False, "error": "id 不能为空（task_list 里的编号，或一句只对上唯一一条的关键词）"}
        rows = await asyncio.to_thread(self.rt.storage.list_tasks, sid, "pending", 50)
        target = None
        if ref.isdigit():
            target = next((r for r in rows if int(r["id"]) == int(ref)), None)
            if target is None:
                one = await asyncio.to_thread(self.rt.storage.get_task, int(ref))
                gone = f"（当前状态 {one['status']}）" if one else ""
                return {"ok": False, "error": f"#{ref} 不是本会话待触发的任务{gone}"}
        else:
            hits = [r for r in rows if ref in (r["instruction"] or "")]
            if not hits:
                return {"ok": False, "error": f"指令里没有「{ref}」的待触发任务，先 task_list 看编号"}
            if len(hits) > 1:
                ids = "、".join(f"#{r['id']}（{clip(r['instruction'], 20)}）" for r in hits[:5])
                return {"ok": False, "error": f"「{ref}」对上 {len(hits)} 条，说清编号：{ids}"}
            target = hits[0]
        actor = ctx.get("actor") or {}
        oid = str(actor.get("openid") or "")
        if oid not in (target["owner"], target["notify"]) and int(actor.get("level") or 0) < 5:
            return {"ok": False, "error": "只能取消自己设的、或汇报给自己的那条"}
        await asyncio.to_thread(self.rt.storage.finish_task, target["id"], "cancelled", _now_text())
        out = {"ok": True, "cancelled": target["id"], "due": fmt_due(target["due_ts"]),
               "instruction": target["instruction"], "note": "已取消，到点不会再跑"}
        if target.get("rule"):
            out["repeat"] = rule_label(target["rule"])
            out["note"] = (f"已取消整条{rule_label(target['rule'])}任务（#{target['id']}），"
                           f"已跑过 {int(target.get('fired_count') or 0)} 场，后面不再跑")
        return out

    # ---------- 触发（巡检循环） ----------

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
        """扫一遍到期的任务，返回这一轮真正跑完的场数（汇报了或选择沉默都算）。"""
        if self.run_task is None:
            return 0  # 工具侧的实例不触发，交给巡检循环
        cfg = self.config
        grace = max(0, int(cfg.get("miss_grace_sec", 300) or 0))
        now = int(time.time())
        ran = 0
        for row in await asyncio.to_thread(self.rt.storage.due_tasks, now):
            extra = {"session": session_label(row["session_id"])}
            late = now - int(row["due_ts"])
            rule = row.get("rule") or ""
            if late > grace:
                if rule:
                    nxt, skipped = self._catch_up(int(row["due_ts"]), rule, now)
                    await asyncio.to_thread(self.rt.storage.skip_task, row["id"], nxt)
                    logger.warning("任务跳过 %d 场未跑（错过 %ds，宽限 %ds）#%d: %s",
                                   skipped, late, grace, row["id"],
                                   clip(row["instruction"], 40), extra=extra)
                    continue
                await asyncio.to_thread(self.rt.storage.finish_task, row["id"], "expired")
                logger.warning("任务过期未跑（错过 %ds，宽限 %ds）#%d: %s",
                               late, grace, row["id"], clip(row["instruction"], 40), extra=extra)
                continue
            state = await self.run_task(row["session_id"], row)
            if state == "blocked":
                continue  # 频控拦住：下一轮再试，超宽限自然转 expired
            if state in _FAIL_REASONS:
                await self._fail(row, _FAIL_REASONS[state], extra)
                continue
            nxt, done_times = await self._settle(row)
            ran += 1
            said = "已汇报" if state == "sent" else "没什么可说"
            if nxt:
                logger.info("任务到期跑完 #%d 第 %d 场（%s，下一场 %s，%s）",
                            row["id"], int(row.get("fired_count") or 0) + 1, rule_label(rule),
                            fmt_due(nxt), said, extra=extra)
            else:
                tail = "，已排满" if done_times else ""
                logger.info("任务到期跑完 #%d（汇报给 %s，%s%s）", row["id"], self._who_label(row),
                            said, tail, extra=extra)
        return ran

    async def _fail(self, row: dict, reason: str, extra: dict) -> None:
        """一场没跑成：记一笔失败，连着失败到上限就停掉这条任务。"""
        cap = max(1, int(self.config.get("max_failures", 5) or 5))
        res = await asyncio.to_thread(self.rt.storage.fail_task, row["id"], reason, cap)
        if res["terminal"]:
            logger.warning("任务 #%d 连着失败 %d 次，已停（%s）: %s", row["id"], res["count"],
                           reason, clip(row["instruction"], 40), extra=extra)
        else:
            logger.warning("任务 #%d 失败 %d/%d 次（%s），下一轮再试", row["id"], res["count"],
                           cap, reason, extra=extra)

    async def _settle(self, row: dict) -> tuple[int, bool]:
        """一场跑完：周期项记一次并把到期推到下一场，一次性项或排满场次的直接置 fired。

        返回（下一场的到期时间, 是不是因为排满场次才结束）；调用方只拿它写日志，
        状态已经在这一步落进表里，所以跑完与落库不会分成两套口径。
        """
        now_text = _now_text()
        rule = row.get("rule") or ""
        if not rule:
            await asyncio.to_thread(self.rt.storage.finish_task, row["id"], "fired", now_text)
            return 0, False
        nxt = advance(int(row["due_ts"]), rule)
        times = int(row.get("times") or 0)
        done_times = times > 0 and int(row.get("fired_count") or 0) + 1 >= times
        if not nxt or done_times:
            # 这最后一场也跑完了，所以场数要记上，不能只置状态
            await asyncio.to_thread(self.rt.storage.complete_task, row["id"], now_text)
            return 0, done_times
        await asyncio.to_thread(self.rt.storage.reschedule_task, row["id"], nxt, now_text)
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
        total = self.rt.storage.count_tasks(status=None)
        over = max(0, total - max_rows)
        culled = self.rt.storage.delete_oldest_terminal(over) if over else 0
        left = total - culled
        logger.info("任务清理 保留 %d 天 · 到期删 %d · 超限删 %d（共 %d 条）",
                    keep_days, aged, culled, left)
        return {"aged": aged, "over": culled, "total": left, "keep_days": keep_days}

    async def run(self) -> None:
        """后台巡检循环：间隔配在 scan_interval_sec，扫表即跑，停机期间错过的按宽限处置。"""
        self._sweep_at = time.monotonic()
        while True:
            await asyncio.sleep(max(2.0, float(self.config.get("scan_interval_sec", 15) or 15)))
            try:
                await self.tick()
            except Exception as e:  # noqa: BLE001 - 巡检不能因一次失败就停
                logger.warning("任务巡检失败: %s", clip(str(e), 80))
            interval = max(60, int(self.config.get("sweep_interval_sec", 21600) or 21600))
            if time.monotonic() - self._sweep_at >= interval:
                self._sweep_at = time.monotonic()
                try:
                    await asyncio.to_thread(self.sweep)
                except Exception as e:  # noqa: BLE001
                    logger.warning("任务历史清理失败: %s", clip(str(e), 80))


class _TaskTool(Tool):
    data_name = DATA_KEY
    default_config = DEFAULT_CONFIG

    def __init__(self, rt, tasks: Tasks):
        super().__init__(rt)
        self.tasks = tasks


class TaskAdd(_TaskTool):
    name = "task_add"
    min_level = 4
    description = (
        "定时任务：到点按 instruction 真去做一件事（不是到点念一句台词），"
        "用来兑现「每天 18 点看下这个网站的价格，变了就告诉我」「每 10 分钟盯一次有没有更新」"
        "「明天 8 点提醒我交材料」这类约定。"
        "到点跑的是完整的一轮你：能 fetch/search 查网页、kv_get/kv_set 存取上次的数据算增量，"
        "再决定要不要汇报。"
        f"when 只认固定写法，自然语言（明天下午三点）先调 time 拿到今天日期再换算成绝对时间：{WHEN_HELP}。"
        f"要周期就填 repeat：{REPEAT_HELP}；repeat 填了才用 times 限制排几场（0=一直排到被取消）。"
        "instruction 写成到点该做的事：查什么、跟什么比、什么情况下要汇报"
        "（「查 xxx 的今日价格，跟 kv 里昨天那条比，涨了就报，没变就不说」）。"
        "默认汇报给说话的人本人；对方明确让你汇报给别人才填 who（只认这个会话里出现过的昵称）。"
        "设好后在正文里回一句约好的时间（周期就说清多久一次），别设完不作声"
    )
    parameters = {
        "type": "object",
        "properties": {
            "when": {"type": "string", "description": "第一次什么时候跑：+30m / +2h / +1d，或 18:30 / 10-30 18:30 / 2026-10-30 18:30"},
            "instruction": {"type": "string", "description": "到点要去做的事，说清查什么、跟什么比、什么时候汇报"},
            "repeat": {"type": "string", "description": "可选：daily / weekdays / weekly / monthly / every:30m，不填就是一次性"},
            "times": {"type": "integer", "description": "可选（只有填了 repeat 才生效）：排几场，0 或不填=一直排到被取消"},
            "who": {"type": "string", "description": "可选：汇报给别人的昵称，默认汇报给本人"},
        },
        "required": ["when", "instruction"],
    }

    async def run(self, args: dict, ctx: dict) -> dict:
        return await self.tasks.add(args, ctx)


class TaskList(_TaskTool):
    name = "task_list"
    min_level = 4
    config_owner = False
    toggleable = False
    description = (
        "看本会话挂着哪些定时任务（默认只看还没结束的，按下一次到期时间排序，"
        "周期项会带上「每天/每30分/…」和已跑几场，连着失败的会带上次数与原因）。"
        "对方问「我让你盯着什么来着」「还有几个任务」时调用，照返回的编号和时间说"
    )
    parameters = {
        "type": "object",
        "properties": {
            "status": {"type": "string", "description": "可选：all 连已结束/已取消/已过期/失败的历史一起看，默认只看待触发的"},
            "limit": {"type": "integer", "description": "返回条数，默认 20，最多 50"},
        },
    }

    async def run(self, args: dict, ctx: dict) -> dict:
        return await self.tasks.list_rows(args, ctx)


class TaskDel(_TaskTool):
    name = "task_del"
    min_level = 4
    config_owner = False
    toggleable = False
    description = (
        "取消一条还没结束的定时任务（置为已取消，记录仍保留）。id 填 task_list 里的编号；"
        "指令只对上唯一一条时也可以直接给那句关键词。"
        "取消周期项等于整条停下（后面不再跑，已跑过的场次还在）。"
        "只能取消自己设的、或汇报给自己的那条；已经跑完的一次性任务取消不了"
    )
    parameters = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "任务编号，或一句只对上唯一一条的关键词"},
        },
        "required": ["id"],
    }

    async def run(self, args: dict, ctx: dict) -> dict:
        return await self.tasks.cancel(args, ctx)


def get_tools(rt):
    tasks = Tasks(rt)
    return [TaskAdd(rt, tasks), TaskList(rt, tasks), TaskDel(rt, tasks)]
