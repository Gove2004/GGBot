"""Presence 状态机：每会话独立 在线/离线 作息、唤醒、积压只读、抖动聚合、选择性回复。

离线（默认 10 分钟，带 ±40% 抖动）：普通消息仅入库积压（≤1000）；收到@（本会话）
或倒计时结束 → 本会话上线。
在线（默认 1 分钟，带 ±30% 抖动）：上线只把积压标记为 seen 读入上下文、不回复；
消息进入 5s 抖动窗口聚合 → 选择性回复（@/私聊走被动回复；普通群消息走主动消息，
受平台每分钟/每日频控与本地 proactive_* 上限约束）；每成功回复一轮固定续期
renew_per_reply_sec（默认 30s，封顶 renew_max_sec）；倒计时结束 → 下线。
拟人化：回复前有阅读停顿，发送前按内容长度模拟打字耗时（可用 behavior.reply 关闭）。
会话相互独立：私聊唤醒不会让群聊上线，A 群唤醒不影响 B 群。
"""

from __future__ import annotations

import asyncio
import logging
import random
import threading
import time

from tool import agent_loop
from ggbot.util.human import jitter, read_delay_sec, sample_duration
from ggbot.util.session import oid8, session_label

logger = logging.getLogger("ggbot.presence")

SYSTEM_TEMPLATE = """你是 QQ 群/私聊里的聊天伙伴，昵称“{name}”，不是客服也不是助手。

【聊天规则】
{rules}

【相关记忆】（过往片段，可能与当前话题无关，自行判断是否采用）
{memory_digest}

【工具提示】（由各 Tool 自己挂载）
{tool_hints}

【行动准则】
1. 口语化短句，一条不超过 40 字；不要用换行或空行把几句话拼成一条，要说多句就分多次调用 send_text。
2. 纯文本输出不会发送给任何人，必须调用工具。不想说话就别调用工具，直接结束本轮。
3. 被@时必须回应。
4. 图片/语音/视频的地址标注在消息里，可以原样转发（send_image 等）；你看不到图片内容，只能凭上下文回应，不要假装看见了。
5. 想不起的事先用 memory_search 查；用户让你记住、或出现值得长期留存的信息时，用 memory_write 写进去。
6. 群里的普通消息（没@你）也可以接话，但只在你有话想说时；不想接就用 end_call。
7. 用户要求发邮件、语音、表情时直接调用对应工具，不要在调用前就断言做不到；工具失败会返回具体原因。
8. 只聊当下，不翻旧账；离线积压的消息只是让你知道期间发生了什么，不需要逐条回应。
"""


def _derive_target(sid: str) -> tuple[str, str]:
    """从 session_id（如 group:g_xxx / c2c:u_xxx）反推发送目标与场景。

    积压消息行仅持久化 session_id，target/scene 需由此派生。
    """
    sid = sid or ""
    if ":" in sid:
        prefix, _, rest = sid.partition(":")
        return rest, ("group" if prefix == "group" else "c2c")
    return sid, "group"


def _fmt_user_messages(msgs: list[dict]) -> str:
    """把一条或多条入站消息格式化为模型输入（发送者只用 8 位 id，省上下文）。"""
    lines = []
    for m in msgs:
        parts = [f"[{oid8(m.get('sender'))}]"]
        if m.get("is_at"):
            parts.append("(艾特了你)")
        content = m.get("content") or ""
        parts.append(content or "（无文本）")
        for a in m.get("attachments", []) or []:
            ct = (a.get("content_type") or "").lower()
            url = a.get("url") or ""
            # 已落地的本地副本可直接作为 send_image/send_voice 的 source 转发
            local = a.get("local_path") or ""
            ref = f" url={url}" if url else ""
            if local:
                ref += f" local={local}"
            if "image" in ct:
                parts.append(f"[图片{ref}]")
            elif "video" in ct:
                parts.append(f"[视频{ref}]")
            elif "voice" in ct:
                parts.append(f"[语音{ref}]")
            else:
                parts.append(f"[文件 {a.get('filename')}{ref}]")
        lines.append(" ".join(parts))
    return "\n".join(lines)


def _fmt_history(history: list[dict]) -> str:
    """历史对话纯文本（摘要用）：用户按 8 位 id，助手按“我”。"""
    lines = []
    for h in history:
        if h.get("role") == "assistant":
            lines.append(f"[我] {h.get('content') or ''}")
        else:
            lines.append(f"[{oid8(h.get('sender'))}] {h.get('content') or ''}")
    return "\n".join(lines)


class PresenceManager:
    """在线/离线调度 + 消息处理主循环。"""

    def __init__(self, cfg, storage, llm, executor, bot, memory=None):
        self.cfg = cfg
        self.storage = storage
        self.llm = llm
        self.executor = executor
        self.bot = bot
        self.memory = memory

        self.p = cfg.presence
        self.r = cfg.reply
        self.b = cfg.backlog
        self._name = cfg.llm.get("bot_name", "机器人")
        ctx_cfg = cfg.context
        self._max_input = int(ctx_cfg.get("max_input_tokens", 128000))
        self._budget_tokens = int(ctx_cfg.get("summary_threshold_tokens", 100000))
        self._history_limit = int(ctx_cfg.get("max_history_messages", 50))

        self.state = "offline"  # 兼容旧字段（面板聚合时不再使用单值）
        self._states: dict[str, _SessionState] = {}  # 每会话独立状态机
        self._loop_task: asyncio.Task | None = None
        self._lock = threading.RLock()
        self._max_states = 200  # 会话状态对象上限，超出清理离线无积压的
        # 主动消息本地计数：平台群聊硬限制 20 条/分钟、1000 条/天，这里再留余量
        self._active_turns: dict[str, list[float]] = {}
        self._active_day: dict[str, tuple[str, int]] = {}

    def _state_of(self, sid: str) -> "_SessionState":
        """获取（或创建）会话状态机；超上限时清理最久未活动的离线会话。"""
        st = self._states.get(sid)
        if st is not None:
            return st
        with self._lock:
            if len(self._states) >= self._max_states:
                for k, s in list(self._states.items()):
                    if s.state == "offline" and not self.storage.get_pending_messages(k, 1):
                        self._states.pop(k, None)
                        break
            st = _SessionState(self, sid)
            self._states[sid] = st
        return st

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        self._loop_task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except (asyncio.CancelledError, Exception):
                pass
        for st in list(self._states.values()):
            if st._debounce_task:
                st._debounce_task.cancel()
                try:
                    await st._debounce_task
                except (asyncio.CancelledError, Exception):
                    pass

    async def forget_session(self, sid: str) -> None:
        """丢弃会话运行态（清空上下文/重置后调用），不影响其他会话。"""
        with self._lock:
            st = self._states.pop(sid, None)
        if st and st._debounce_task:
            st._debounce_task.cancel()
            try:
                await st._debounce_task
            except (asyncio.CancelledError, Exception):
                pass

    async def _loop(self) -> None:
        """统一 tick：每 2 秒扫一遍全部会话状态机，触发到期上线/下线。"""
        while True:
            await self._tick()
            await asyncio.sleep(2)

    async def _tick(self) -> None:
        now = time.monotonic()
        for st in list(self._states.values()):
            if st.state == "offline":
                if st._next_online <= now:
                    await st.go_online("scheduled")
            elif st._online_deadline <= now:
                await st.go_offline("timeout")

    # ---------- 消息入口 ----------

    async def handle_message(self, inbound: dict) -> bool:
        """处理一条入站消息。返回 True 表示消息已被机器人消费。"""
        sid = self.storage.get_or_create_session(inbound["key"], inbound.get("scene", "group"))
        inbound["session_id"] = sid
        self.storage.add_message(
            sid, "user", inbound["sender"], inbound["content"],
            inbound.get("attachments"), inbound["msg_id"], inbound.get("msg_seq"),
            inbound.get("event_id"), inbound.get("is_at", False), inbound.get("passive_ok", False),
            status="pending",
        )
        st = self._state_of(sid)
        st.touch()
        if st.state == "offline":
            if inbound.get("is_at") and self.p.get("wake_on_at_message", True):
                # 先读入离线积压（不含本条），让本次回复能看到期间发生了什么；
                # 旧消息只标记 seen 不回复，本条@仍走即时被动回复
                seen = self._read_backlog_sid(sid, exclude_msg_id=inbound.get("msg_id"))
                await st.go_online("wake-on-at", seen=seen)
                if await self._reply_now(inbound):
                    st.renew()
                return True
            pending = self.storage.count_pending(sid)
            logger.debug("离线消息入库（pending=%d）", pending, extra={"session": st.label})
            return True
        # 在线：进入本会话 debounce 窗口聚合
        st.debounce.append(inbound)
        logger.debug("在线入窗 窗口=%d", len(st.debounce), extra={"session": st.label})
        if st._debounce_task is None or st._debounce_task.done():
            st._debounce_task = asyncio.create_task(self._flush_debounce(st))
        return True

    async def _flush_debounce(self, st: "_SessionState") -> None:
        await asyncio.sleep(float(self.r.get("debounce_sec", 5)))
        batch = st.debounce
        st.debounce = []
        if not batch:
            st._debounce_task = None
            return
        st._processing = True
        try:
            await self._process_batch(batch, st)
        finally:
            st._processing = False
            st._debounce_task = None
            # 处理期间又来了新消息：等这一轮结束再开下一轮，避免两轮并发、回复乱序
            if st.debounce and st.state == "online":
                st._debounce_task = asyncio.create_task(self._flush_debounce(st))

    # ---------- 在线/离线（会话级） ----------

    def _session_label(self, sid: str) -> str:
        return session_label(sid)

    async def _process_batch(self, msgs: list[dict], st: "_SessionState" | None = None) -> None:
        """在线批次：聚合多条消息一次决策并选择性回复。

        @/C2C 批次带 msg_id+event_id 走被动回复（不耗额度）；
        普通群消息批次只能走主动消息（2026-06-22 起群主动推送已全量开放，
        群主需开启「机器人主动在群聊内发言」），受平台 20 条/分钟、1000 条/天
        与本地 proactive_per_minute/proactive_per_day 约束，超限则本轮不回复。
        """
        passive = any(m.get("passive_ok") for m in msgs)
        sid = msgs[-1].get("session_id") or ""
        label = st.label if st else session_label(sid)
        if not passive:
            blocked = self._active_block_reason(sid)
            if blocked:
                logger.debug("在线普通消息批次跳过回复（%s）%d 条", blocked, len(msgs),
                             extra={"session": label})
                return
        if await self._safe_reply(msgs, passive=passive):
            if not passive:
                self._note_active(sid)
            if st is not None:
                st.renew()

    def _active_block_reason(self, sid: str) -> str | None:
        """主动消息本地闸门：返回拦截原因，None 表示可发。"""
        if not self.p.get("proactive_enabled", True):
            return "主动消息已关闭"
        now = time.monotonic()
        window = [t for t in self._active_turns.get(sid, []) if now - t < 60]
        self._active_turns[sid] = window
        if len(window) >= int(self.p.get("proactive_per_minute", 6)):
            return "主动消息每分钟上限"
        day = time.strftime("%Y-%m-%d")
        d, n = self._active_day.get(sid, (day, 0))
        if n >= int(self.p.get("proactive_per_day", 1000)) and d == day:
            return "主动消息每日上限"
        return None

    def _note_active(self, sid: str) -> None:
        self._active_turns.setdefault(sid, []).append(time.monotonic())
        day = time.strftime("%Y-%m-%d")
        d, n = self._active_day.get(sid, (day, 0))
        self._active_day[sid] = (day, n + 1 if d == day else 1)

    # ---------- 回复核心 ----------

    def _target_of(self, sid: str) -> tuple[str, str]:
        """会话发送目标：从 sessions.key 反查完整 openid，失败回退旧推导。"""
        t = self.storage.session_target(sid)
        if t and t[0]:
            return t
        return _derive_target(sid)

    async def _safe_reply(self, msgs: list[dict], passive: bool) -> bool:
        """工具制回合：LLM 通过 send_text 等工具实际发送消息。

        状态判定：
        - 回合内工具实际发送成功（ctx.sent_any）→ replied（续期）；
        - 任一工具执行报错（ctx.tool_error）→ failed；
        - 未调用工具 / end_call / 无话可说 → skipped。
        任何失败只标记状态，不向上抛（避免单条失效群打崩整轮积压）。
        """
        ctx_msg = msgs[-1]
        sid = ctx_msg["session_id"]
        label = session_label(sid)
        ids = [m["msg_id"] for m in msgs if m.get("msg_id")]

        def _mark(status: str) -> None:
            self.storage.mark_pending_processed(ids, status)

        # 拟人化：先“看一眼”消息再处理（批量积压不额外等待，避免拖过被动窗口）
        if passive and len(msgs) <= 3:
            delay = read_delay_sec(self.r)
            if delay > 0:
                await asyncio.sleep(delay)

        ctx: dict = {}
        try:
            await self._agent_loop(sid, msgs, ctx_msg, ctx)
        except Exception as e:  # noqa: BLE001
            logger.warning("回复生成失败 n=%d: %s", len(msgs), e, extra={"session": label})
            _mark("failed")
            return False
        if ctx.get("sent_any"):
            _mark("replied")
            return True
        if ctx.get("tool_error"):
            if passive:
                logger.warning("工具执行失败 n=%d: %s", len(msgs), ctx["tool_error"],
                               extra={"session": label})
            else:
                logger.warning("主动回复失败 n=%d（群主需开启「机器人主动在群聊内发言」）: %s",
                               len(msgs), ctx["tool_error"], extra={"session": label})
            _mark("failed")
            return False
        _mark("skipped")
        return False

    async def _reply_now(self, inbound: dict) -> bool:
        """唤醒即时回复：单条消息直接走 agent 循环；返回是否实际发送。"""
        return await self._safe_reply([inbound], passive=True)

    def _read_backlog_sid(self, sid: str, exclude_msg_id: str | None = None) -> int:
        """上线只读积压：本会话 pending 标记为 seen 读入上下文，绝不回复。

        seen 消息会作为历史参与后续生成，但不会单独触发 LLM 回合；
        被@唤醒时先读入旧积压再回复本条，因此回复里能自然看到离线期间的内容。
        """
        ids = [
            m["msg_id"] for m in self._pending_of(sid)
            if m.get("msg_id") and m["msg_id"] != exclude_msg_id
        ]
        self.storage.mark_pending_processed(ids, "seen")
        return len(ids)

    def _pending_of(self, sid: str) -> list[dict]:
        return self.storage.get_pending_messages(sid, int(self.b.get("max_scan_messages", 1000)))

    # ---------- 上下文组装 ----------

    async def _agent_loop(self, sid: str, msgs: list[dict], ctx_msg: dict, ctx: dict | None = None) -> str:
        ctx = ctx if ctx is not None else {}
        system = await self._build_system(sid, msgs[-1] if msgs else ctx_msg)
        history = self.storage.get_recent_messages(sid, self._history_limit)
        history_msgs = []
        for h in history:
            if (h.get("status") or "") == "pending":
                # 未处理的积压消息不进历史：即时回复时避免 LLM 顺带回答积压、
                # 随后积压处理又正式回复一遍（表现为"重复回复旧消息"）
                continue
            if h["role"] == "user":
                history_msgs.append({"role": "user", "content": f"[{oid8(h['sender'])}] {h['content']}"})
            else:
                history_msgs.append({"role": "assistant", "content": h["content"]})
        user_content = "【本轮消息】（只回应这里的内容）\n" + _fmt_user_messages(msgs)
        messages = [{"role": "system", "content": system}, *history_msgs, {"role": "user", "content": user_content}]

        # 上下文预算：超过 summary_threshold_tokens 用 LLM 摘要压缩历史
        budget = self._budget_tokens
        total = self.llm.estimate_tokens(system) + sum(
            self.llm.estimate_tokens(m.get("content") or "") for m in messages
        )
        label = session_label(sid)
        logger.debug("agent 上下文: history=%d tokens≈%d 预算=%d",
                     len(history_msgs), total, budget, extra={"session": label})
        if total > budget and history:
            summary = await asyncio.to_thread(self.llm.summarize, _fmt_history(history))
            self.storage.save_summary(sid, summary)
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": f"【更早的历史摘要】{summary}\n\n{user_content}"},
            ]
            logger.info("上下文超限，历史压缩为摘要（%s tokens）", total, extra={"session": label})

        ctx["target"] = ctx_msg.get("target") or self._target_of(sid)[0]
        ctx["scene"] = ctx_msg.get("scene") or self._target_of(sid)[1]
        # 被动回复锚点：批次里最后一条可被动回复的消息；没有（纯普通消息批次）则主动发送
        anchor = next((m for m in reversed(msgs) if m.get("passive_ok")), None)
        ctx["msg_id"] = anchor.get("msg_id") if anchor else None
        ctx["event_id"] = anchor.get("event_id") if anchor else None
        ctx["msg_seq"] = 1
        ctx["session_id"] = sid
        return await agent_loop(self.llm, self.executor, messages, ctx)

    async def _build_system(self, sid: str, ctx_msg: dict) -> str:
        rules = self.storage.list_rules(active_only=True)
        rules_text = "\n".join(f"- {r['content']}" for r in rules) or "（无）"
        # 记忆注入：当前消息为 query，检索全局 + 本会话记忆；失败不阻塞回复
        query = (ctx_msg.get("content") or "").strip()
        digest = "（无）"
        if query and self.memory is not None:
            try:
                hits = await asyncio.wait_for(
                    asyncio.to_thread(self.memory.search, query, 3, sid, 0.35), timeout=5.0
                )
                lines = [f"- {(h.get('content') or '')[:80]}" for h in hits]
                digest = "\n".join(lines) or "（无）"
            except Exception as e:  # noqa: BLE001
                logger.debug("记忆检索失败/超时，跳过注入: %s: %s", type(e).__name__, e)
        return SYSTEM_TEMPLATE.format(
            name=self._name, memory_digest=digest, rules=rules_text,
            tool_hints=self.executor.hints(),
        )

    # ---------- 面板状态 ----------

    def status(self) -> dict:
        """聚合各会话状态，供 Web 面板展示。"""
        now = time.monotonic()
        sessions = []
        for sid, st in self._states.items():
            if st.state == "online":
                remaining = max(0.0, st._online_deadline - now)
                next_event = "离线倒计时"
            else:
                remaining = max(0.0, st._next_online - now)
                next_event = "上线倒计时"
            sessions.append({
                "sid": sid,
                "label": self._session_label(sid),
                "state": st.state,
                "reason": st.reason,
                "next_event": next_event,
                "remaining_sec": round(remaining, 1),
                "idle_sec": round(max(0.0, now - st._last_active), 1),
                "debounce_count": len(st.debounce),
            })
        sessions.sort(key=lambda s: (s["state"] != "online", s["sid"]))
        online = sum(1 for s in sessions if s["state"] == "online")
        # 顶层聚合字段（兼容旧前端）：优先第一个在线会话的倒计时，否则第一个会话
        head = next((s for s in sessions if s["state"] == "online"), sessions[0] if sessions else None)
        return {
            "state": "online" if online else "offline",  # 任一在线即 online
            "online_sessions": online,
            "sessions": sessions,
            "next_event": head["next_event"] if head else "—",
            "remaining_sec": head["remaining_sec"] if head else 0,
            "debounce_count": sum(s["debounce_count"] for s in sessions),
            "pending_count": self.storage.count_pending(),
            "online_duration_sec": self.p.get("online_duration_sec"),
            "offline_interval_sec": self.p.get("offline_interval_sec"),
            "online_duration_jitter": self.p.get("online_duration_jitter", 0.3),
            "offline_interval_jitter": self.p.get("offline_interval_jitter", 0.4),
            "renew_on_reply": self.p.get("renew_on_reply"),
            "renew_per_reply_sec": self.p.get("renew_per_reply_sec", 30),
            "renew_max_sec": self.p.get("renew_max_sec", 600),
            "renew_jitter": self.p.get("renew_jitter", 0.3),
            "read_delay_min_sec": self.r.get("read_delay_min_sec", 0),
            "read_delay_max_sec": self.r.get("read_delay_max_sec", 0),
            "typing_chars_per_sec": self.r.get("typing_chars_per_sec", 0),
            "debounce_sec": self.r.get("debounce_sec"),
            "backlog_max_scan": self.b.get("max_scan_messages"),
        }


class _SessionState:
    """单会话 在线/离线 状态机。

    离线：普通消息积压；收到@或倒计时结束 → 上线（只读积压，不回复）。
    在线：消息进 5s 抖动窗口；每成功回复一轮续期固定时长；倒计时结束下线。
    """

    def __init__(self, pm: "PresenceManager", sid: str):
        self.pm = pm
        self.sid = sid
        self.label = pm._session_label(sid)
        self.state = "offline"
        self.reason = "initial"
        self._online_deadline = 0.0
        self._last_active = time.monotonic()
        # 首次上线在离线周期内随机错开，避免所有会话整点同时上线
        self._next_online = time.monotonic() + self._offline_span() * random.uniform(0.4, 1.0)
        self.debounce: list[dict] = []
        self._debounce_task: asyncio.Task | None = None
        self._processing = False  # 本轮批次是否仍在跑 LLM/工具（防并发两轮）

    @property
    def _p(self) -> dict:
        return self.pm.p

    def _online_span(self) -> float:
        """本次在线时长：基准 ±online_duration_jitter。"""
        return sample_duration(
            float(self._p.get("online_duration_sec", 60)),
            float(self._p.get("online_duration_jitter", 0.3)),
            minimum=5.0,
        )

    def _offline_span(self) -> float:
        """本次离线时长：基准 ±offline_interval_jitter。"""
        return sample_duration(
            float(self._p.get("offline_interval_sec", 600)),
            float(self._p.get("offline_interval_jitter", 0.4)),
            minimum=10.0,
        )

    def touch(self) -> None:
        self._last_active = time.monotonic()

    async def go_online(self, reason: str, seen: int | None = None) -> None:
        if self.state == "online":
            return
        self.state = "online"
        self.reason = reason
        span = self._online_span()
        self._online_deadline = time.monotonic() + span
        self.touch()
        if seen is None:
            seen = self.pm._read_backlog_sid(self.sid)
        label = {"scheduled": "自然上线", "wake-on-at": "强制上线"}.get(reason, "上线")
        logger.info("%s 预计在线 %.0fs, 积压消息 %d 条", label, span, seen,
                    extra={"session": self.label})

    async def go_offline(self, reason: str) -> None:
        if self.state == "offline":
            return
        self.state = "offline"
        self.reason = reason
        wait = self._offline_span()
        self._next_online = time.monotonic() + wait
        # 清理本会话未发送的抖动批次（下线即停，避免残留任务/消息堆积）；
        # 正在处理中的批次不打断，避免回复发到一半被取消
        if self._debounce_task and not self._processing:
            self._debounce_task.cancel()
            try:
                await self._debounce_task
            except (asyncio.CancelledError, Exception):
                pass
            self._debounce_task = None
        self.debounce = []
        logger.info("下线 预计离线 %.0fs", wait, extra={"session": self.label})

    def renew(self) -> None:
        """每成功回复一轮：在线时长固定续期（默认 30s，封顶 renew_max_sec）。"""
        if not self._p.get("renew_on_reply", True):
            return
        inc = jitter(float(self._p.get("renew_per_reply_sec", 30)),
                     float(self._p.get("renew_jitter", 0.3)), minimum=0.0)
        cap = max(0.0, float(self._p.get("renew_max_sec", 600)))
        if cap:
            inc = min(inc, cap)
        self._online_deadline = max(self._online_deadline, time.monotonic()) + inc
        self.reason = "reply-renew"
        self.touch()
