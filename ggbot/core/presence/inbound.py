"""入站编排：一条消息进来该走哪条路。

权限闸门 → 入库 → 按这个会话此刻的作息决定去向：刚启动直接上线接话 / 被@立刻回这一条 /
离线只攒积压 / 在线进抖动窗口聚合成一批交给决策层。

计时在 state.py，"能不能发、怎么发"在 decision.py，这一层只管分流与窗口，
上下线日志的口径也只有这一处（每个会话的作息变化都从这里出去）。
"""

from __future__ import annotations

import asyncio
import logging
import random
import threading
import time

from ggbot.core.runtime_stats import runtime
from ggbot.util.session import oid8, session_label

from ggbot.core.presence.state import SessionState

logger = logging.getLogger("ggbot.presence")

# 上线原因在日志里的说法（面板与排查都对得上）
ONLINE_LABEL = {"scheduled": "自然上线", "wake-on-at": "强制上线", "startup": "启动上线"}

TASK_ONLINE_WITHIN_SEC = 30.0
"""任务汇报发出之后，把上线倒计时压到半分钟之内：有人回这句话时不用等整个离线周期。"""


class InboundOrchestrator:
    """会话状态机的宿主：收消息、切上下线、聚合在线窗口。"""

    def __init__(self, cfg, storage, perms, decision, *, max_states: int = 200):
        self.cfg = cfg
        self.storage = storage
        self.perms = perms
        self.decision = decision  # 回合决策：TurnDecider
        self._states: dict[str, SessionState] = {}
        self._debounce: dict[str, list[dict]] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._busy: set[str] = set()  # 这一轮仍在跑 LLM/工具，下线不打断
        self._lock = threading.RLock()
        self._max_states = max_states

    # ---------- 配置现读 ----------

    @property
    def p(self) -> dict:
        return self.cfg.presence

    @property
    def r(self) -> dict:
        return self.cfg.reply

    @property
    def backlog_limit(self) -> int:
        return int(self.cfg.backlog.get("max_scan_messages", 256))

    @property
    def online_limit(self) -> int:
        return int(self.cfg.backlog.get("online_max_messages", 16))

    # ---------- 会话状态机 ----------

    def state_of(self, sid: str) -> SessionState:
        """获取（或创建）会话作息；超上限时清理最久未活动的离线会话。"""
        st = self._states.get(sid)
        if st is not None:
            return st
        with self._lock:
            if len(self._states) >= self._max_states:
                for k, s in list(self._states.items()):
                    if not s.is_online and not self.storage.get_backlog_messages(k, 1):
                        self._states.pop(k, None)
                        break
            st = SessionState(sid, session_label(sid), lambda: self.p)
            self._states[sid] = st
        return st

    async def forget_session(self, sid: str) -> None:
        """丢弃会话运行态（清空上下文/重置后调用），不影响其他会话。"""
        with self._lock:
            st = self._states.pop(sid, None)
        if st:
            await self.cancel_window(sid)

    async def stop(self) -> None:
        for sid in list(self._tasks.keys()):
            await self.cancel_window(sid)
        self._debounce.clear()

    def window(self, sid: str) -> list[dict]:
        """这个会话在线窗口里还没交出去批次（只读，给面板与回归测试看现场）。"""
        return self._debounce.get(sid) or []

    async def cancel_window(self, sid: str) -> None:
        """停掉等待中的抖动窗口（下线/清理会话/停机都要它）。"""
        task = self._tasks.pop(sid, None)
        if task:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    # ---------- 上下线 ----------

    async def go_online(self, st: SessionState, reason: str, seen: int | None = None) -> bool:
        """切到在线：读入积压（只读不回）并记一行上下线日志。返回是否真的切换了。"""
        span = st.go_online(reason)
        if span is None:
            return False
        if seen is None:
            seen = self.read_backlog(st.sid)
        # 空转的定时上线降到 DEBUG：没有积压也没消息，日志里不值得占一行
        log = logger.info if (seen or reason != "scheduled") else logger.debug
        log("%s 预计在线 %.0fs, 积压消息 %d / %d 条",
            ONLINE_LABEL.get(reason, "上线"), span, seen, self.backlog_limit,
            extra={"session": st.label})
        return True

    async def go_offline(self, st: SessionState, reason: str) -> bool:
        """切到离线：清掉窗口里还没发出去的批次（正在跑的那轮不打断，避免回复发到一半被取消）。"""
        span = st.go_offline(reason)
        if span is None:
            return False
        task = self._tasks.get(st.sid)
        if task and st.sid not in self._busy:
            await self.cancel_window(st.sid)
        self._debounce[st.sid] = []
        # 本周期没收到过任何消息 → 上下线只是一次空转，降到 DEBUG
        log = logger.info if st.activity else logger.debug
        log("下线 预计离线 %.0fs（本周期收 %d 条）", span, st.activity, extra={"session": st.label})
        return True

    async def go_online_with_backlog(self, st: SessionState) -> None:
        """自然上线带上积压：旧消息只读进上下文，最后一条当本轮要回的话头。

        其余积压（含超出上限的）置已读，最后一条仍挂在待处理里投进在线抖动窗口——
        上线瞬间又来新消息时会并成同一批次，不会对同一件事回两遍。
        """
        backlog = self.backlog_of(st.sid)
        last = backlog[-1] if backlog else None
        seen = (self.read_backlog(st.sid, exclude_msg_id=last.get("msg_id"))
                if last else None)
        await self.go_online(st, "scheduled", seen=seen)
        if last is None:
            return
        logger.debug("上线带上最后一条积压，交给在线窗口回复", extra={"session": st.label})
        self._append(st.sid, last)
        self._schedule_flush(st)

    async def tick(self) -> None:
        """扫一遍全部会话作息，触发到期的上线/下线。"""
        now = time.monotonic()
        for st in list(self._states.values()):
            if not st.expired(now):
                continue
            if st.is_online:
                await self.go_offline(st, "timeout")
            else:
                await self.go_online_with_backlog(st)

    # ---------- 积压 ----------

    def backlog_of(self, sid: str) -> list[dict]:
        """还挂着「待处理」的用户消息（按到达顺序，最多扫 backlog_limit 条）。"""
        return self.storage.get_backlog_messages(sid, self.backlog_limit)

    def read_backlog(self, sid: str, exclude_msg_id: str | None = None) -> int:
        """上线只读积压：读进上下文、结束回复需求，绝不回复。

        已读消息会作为历史参与后续生成，但不会单独触发 LLM 回合；
        被@唤醒时先读入旧积压再回复本条，因此回复里能自然看到离线期间的内容。
        超出积压上限的旧消息不读入上下文，但同样置为已读，避免永远挂在待处理里。
        """
        ids = self.storage.list_backlog_ids(sid, exclude_msg_id)
        read = self.storage.mark_backlog_read(ids)
        if read:
            logger.debug("离线积压已读 %d 条（只读不回）", read,
                         extra={"session": session_label(sid)})
        return read

    # ---------- 消息入口 ----------

    async def handle_message(self, inbound: dict) -> bool:
        """处理一条入站消息。返回 True 表示消息已被机器人消费。"""
        sid = self.storage.get_or_create_session(inbound["key"], inbound.get("scene", "group"))
        inbound["session_id"] = sid
        st = self.state_of(sid)
        # @ 事件比全量事件晚到时，那行正文已在库里、@ 标记也补好了：这条只用来唤醒，
        # 不重复计数、不再插第二条；在线的话它已经在聚合窗口里，再走一遍会回两次
        late_at = bool(inbound.get("already_stored"))
        if late_at and st.is_online:
            return True
        if not late_at:
            runtime.inbound(sid)
        actor = self.perms.resolve(inbound.get("sender"), inbound.get("sender_name"))
        if actor["level"] <= 0:
            # 0 级禁止：不入库、不进上下文、不触发回合
            logger.debug("忽略禁止用户的消息: %s", actor.get("name") or oid8(actor.get("openid")),
                         extra={"session": session_label(sid)})
            return True
        if not late_at:
            self.storage.add_message(
                sid, "user", inbound["sender"], inbound["content"],
                inbound.get("attachments"), inbound["msg_id"], inbound.get("msg_seq"),
                inbound.get("event_id"), inbound.get("is_at", False), inbound.get("passive_ok", False),
                # 展示名以 actors 表为准（面板改过的叫法优先），没登记过才用这次带过来的 QQ 昵称
                sender_name=actor.get("name") or inbound.get("sender_name"),
            )
        st.touch()
        if (not st.is_online and st.reason == "initial" and self.p.get("start_online", True)):
            # 进程刚启动后该会话的第一条消息：直接上线接话，别先压进一个离线周期
            # （默认离线要等 4–10 分钟定时窗口，重启后看起来像没反应）
            await self.go_online(st, "startup",
                                 seen=self.read_backlog(sid, exclude_msg_id=inbound.get("msg_id")))
        if not st.is_online:
            if inbound.get("is_at") and self.p.get("wake_on_at_message", True):
                # 先读入离线积压（不含本条），让本次回复能看到期间发生了什么；
                # 旧消息只置已读不回复，本条@仍走即时被动回复
                await self.go_online(st, "wake-on-at",
                                     seen=self.read_backlog(sid, exclude_msg_id=inbound.get("msg_id")))
                st.activity += 1
                await self.decision.reply_now(inbound, st)
                return True
            pending = self.storage.count_backlog(sid)
            logger.debug("离线积压 %d / %d 条（@可立刻唤醒）", pending, self.backlog_limit,
                         extra={"session": st.label})
            return True
        # 在线：进入本会话抖动窗口聚合；窗口最多保留 online_limit 条，更早的只读不回
        st.activity += 1
        overflow = self._append(st.sid, inbound)
        if overflow:
            logger.debug("在线窗口超出上限 %d，最旧 %d 条只读不回",
                         self.online_limit, len(overflow), extra={"session": st.label})
        self._schedule_flush(st)
        return True

    def _append(self, sid: str, msg: dict) -> list[dict]:
        """进窗口并按上限裁掉最旧的几条，返回被裁掉的消息（已置已读）。"""
        buf = self._debounce.setdefault(sid, [])
        buf.append(msg)
        overflow = len(buf) - self.online_limit
        if overflow <= 0:
            return []
        dropped = buf[:overflow]
        del buf[:overflow]
        self.storage.mark_backlog_read([m["msg_id"] for m in dropped if m.get("msg_id")])
        return dropped

    def _schedule_flush(self, st: SessionState) -> None:
        task = self._tasks.get(st.sid)
        if task is None or task.done():
            self._tasks[st.sid] = asyncio.create_task(self._flush_after_jitter(st))

    async def _flush_after_jitter(self, st: SessionState) -> None:
        """5–10 秒随机抖动：窗口里攒够一批再交出去，一次决策回一条，避免逐条应答。"""
        lo = float(self.r.get("debounce_sec", 5))
        hi = float(self.r.get("debounce_max_sec", 10))
        await asyncio.sleep(random.uniform(min(lo, hi), max(lo, hi)))
        batch = self._debounce.get(st.sid) or []
        self._debounce[st.sid] = []
        self._tasks.pop(st.sid, None)
        if not batch:
            return
        logger.debug("在线窗口聚合 %d 条，开始决策", len(batch), extra={"session": st.label})
        self._busy.add(st.sid)
        try:
            await self.decision.process_batch(batch, st)
        finally:
            self._busy.discard(st.sid)
            # 处理期间又来了新消息：等这一轮结束再开下一轮，避免两轮并发、回复乱序
            if self._debounce.get(st.sid) and st.is_online:
                self._schedule_flush(st)

    # ---------- 回合外：定时任务 ----------

    async def run_task(self, sid: str, task: dict) -> str:
        """任务到点就跑一个完整回合（决策层组装上下文、模型调工具），返回这一场的结果状态。

        真发出去了才把上线倒计时压到半分钟内：有人回这句话时不用等整个离线周期。
        积压交给正常的上线流程读，这里不抢读，免得把还没回的消息标成已读。
        """
        st = self.state_of(sid)
        state = await self.decision.run_task(sid, task, st)
        if state == "sent" and not st.is_online:
            st.hurry_online(TASK_ONLINE_WITHIN_SEC)
        return state

    # ---------- 面板 ----------

    def panel_rows(self) -> list[dict]:
        """各会话作息快照（面板展示用），在线的排前面。"""
        now = time.monotonic()
        rows = []
        for sid, st in self._states.items():
            next_event, remaining = st.countdown(now)
            rows.append({
                "sid": sid,
                "label": st.label,
                "state": st.state,
                "reason": st.reason,
                "next_event": next_event,
                "remaining_sec": round(remaining, 1),
                "idle_sec": round(max(0.0, now - st.last_active), 1),
                "debounce_count": len(self._debounce.get(sid) or []),
            })
        rows.sort(key=lambda s: (s["state"] != "online", s["sid"]))
        return rows
