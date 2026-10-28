"""Presence 状态机：每会话独立 在线/离线 作息、唤醒、积压只读、抖动聚合、选择性回复。

离线（默认 10 分钟，带 ±40% 抖动）：普通消息仅入库积压（≤1000）；收到@（本会话）
或倒计时结束 → 本会话上线。
在线（默认 1 分钟，带 ±30% 抖动）：上线只把积压标记为 seen 读入上下文、不回复；
消息进入 5s 抖动窗口聚合 → 选择性回复；每成功回复一轮按 renew_per_reply_sec 递增续期
（连续聊得越久奖励越多，直到 renew_max_sec 上限）；倒计时结束 → 下线。
拟人化：回复前有阅读停顿，发送前按内容长度模拟打字耗时（可用 behavior.reply 关闭）。
会话相互独立：私聊唤醒不会让群聊上线，A 群唤醒不影响 B 群。
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import threading
import time

from ggbot.tool import agent_loop
from ggbot.util.human import jitter, read_delay_sec, sample_duration, typing_delay_sec
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
1. 口语化短句，一条不超过 40 字；要多说几句就多次调用 send_text。
2. 纯文本输出不会发送给任何人，必须调用工具。不想说话就别调用工具，直接结束本轮。
3. 被@时必须回应。
4. 图片/语音/视频的地址标注在消息里，可以直接转发（send_image 等）或点评。
5. 想不起的事用 memory_search 回忆；值得长期记住的用 memory_write。
6. 只聊当下，不翻旧账；离线积压的消息只是让你知道期间发生了什么，不需要逐条回应。
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
            if "image" in ct:
                parts.append(f"[图片 url={url}]")
            elif "video" in ct:
                parts.append(f"[视频 url={url}]")
            elif "voice" in ct:
                parts.append(f"[语音 url={url}]")
            else:
                parts.append(f"[文件 {a.get('filename')} url={url}]")
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


def split_reply(text: str, max_chars: int = 80, max_segments: int = 5) -> list[str]:
    """回复分段：按空行拆段；单段超长按句切，无标点长串按长度硬切；段数超限合并尾部。"""
    parts = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    out: list[str] = []

    def hard_cut(s: str) -> list[str]:
        return [s[i : i + max_chars] for i in range(0, len(s), max_chars)]

    for p in parts:
        if len(p) <= max_chars:
            out.append(p)
            continue
        segs = [s.strip() for s in re.split(r"(?<=[。！？!?])", p) if s.strip()]
        if len(segs) <= 1:
            out.extend(hard_cut(p))
            continue
        buf = ""
        for seg in segs:
            if buf and len(buf) + len(seg) > max_chars:
                out.append(buf)
                buf = seg
            else:
                buf += seg
        if buf:
            out.extend(hard_cut(buf) if len(buf) > max_chars else [buf])
    if len(out) > max_segments and max_segments > 0:
        out = out[: max_segments - 1] + ["\n".join(out[max_segments - 1 :])]
    return out


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
        self._recent_reply_ts = 0.0
        # 回复消息序号：小整数递增（与 msg_id 联合防重；官方默认 1）。
        # 切勿用毫秒时间戳等 int32 超限值——QQ 服务端会 400「请求数据异常」
        self._seq_by_sid: dict[str, int] = {}  # 用于面板展示
        self._max_states = 200  # 会话状态对象上限，超出清理离线无积压的

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
            self._seq_by_sid.pop(sid, None)
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
                logger.debug("收到@，立即唤醒本会话", extra={"session": st.label})
                # 先读入离线积压（不含本条），让本次回复能看到期间发生了什么；
                # 旧消息只标记 seen 不回复，本条@仍走即时被动回复
                self._read_backlog_sid(sid, exclude_msg_id=inbound.get("msg_id"))
                await self._reply_now(inbound)
                await st.go_online("wake-on-at")
                return True
            pending = self.storage.count_pending(sid)
            logger.debug("离线消息入库（pending=%d）", pending, extra={"session": st.label})
            return True
        # 在线：进入本会话 debounce 窗口聚合
        st.debounce.append(inbound)
        logger.debug("在线入窗 msg=%s 窗口=%d", inbound["msg_id"], len(st.debounce),
                     extra={"session": st.label})
        if st._debounce_task is None or st._debounce_task.done():
            st._debounce_task = asyncio.create_task(self._flush_debounce(st))
        return True

    async def _flush_debounce(self, st: "_SessionState") -> None:
        await asyncio.sleep(float(self.r.get("debounce_sec", 5)))
        batch = st.debounce
        st.debounce = []
        st._debounce_task = None
        if not batch:
            return
        await self._process_batch(batch, st)

    # ---------- 在线/离线（会话级） ----------

    async def _go_online(self, reason: str) -> None:
        """兼容入口：作用于当前唯一/默认会话（保留给外部调用，主路径走 _SessionState.go_online）。"""
        st = self._state_of(self._primary_sid())
        await st.go_online(reason)

    def _primary_sid(self) -> str:
        if self._states:
            return next(iter(self._states))
        return "group:default"

    def _session_label(self, sid: str) -> str:
        return session_label(sid)

    async def _process_batch(self, msgs: list[dict], st: "_SessionState" | None = None) -> None:
        """在线批次：聚合多条消息一次决策。

        平台硬限制：全量消息不能被动回复，主动消息每月仅 4 条/群——
        在线聚合的普通消息只进上下文/记忆，不实际发送（"看到没回"也正常）；
        含被动事件（@/C2C）的批次才走回复。
        """
        if not any(m.get("passive_ok") for m in msgs):
            logger.debug("在线普通消息批次跳过回复（主动额度限制）%d 条", len(msgs),
                         extra={"session": st.label if st else session_label(msgs[-1].get("session_id"))})
            return
        if await self._safe_reply(msgs, passive=True, renew_sid=(st.sid if st else None)):
            if st is not None:
                st.renew()

    # ---------- 回复核心 ----------

    def _target_of(self, sid: str) -> tuple[str, str]:
        """会话发送目标：从 sessions.key 反查完整 openid，失败回退旧推导。"""
        t = self.storage.session_target(sid)
        if t and t[0]:
            return t
        return _derive_target(sid)

    async def _safe_reply(self, msgs: list[dict], passive: bool, renew_sid: str | None = None) -> bool:
        """工具制回合：LLM 通过 send_text/seed_message 等工具实际发送消息。

        状态判定：
        - 回合内工具实际发送成功（ctx.sent_any）→ replied（续期）；
        - 发送工具报错（ctx.send_error）→ failed；
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
        if ctx.get("send_error"):
            logger.warning("回复发送失败 n=%d: %s", len(msgs), ctx["send_error"],
                           extra={"session": label})
            _mark("failed")
            return False
        _mark("skipped")
        return False

    async def _reply_now(self, inbound: dict) -> None:
        """唤醒即时回复：单条消息直接走 agent 循环（发送成功即续期）。"""
        await self._safe_reply([inbound], passive=True)

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
        ctx["msg_id"] = ctx_msg.get("msg_id")
        ctx["event_id"] = ctx_msg.get("event_id")  # 工具回复携带，可走被动通道不耗主动额度
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
                logger.debug("记忆检索失败/超时，跳过注入: %s", e)
        return SYSTEM_TEMPLATE.format(
            name=self._name, memory_digest=digest, rules=rules_text,
            tool_hints=self.executor.hints(),
        )

    # ---------- 发送 ----------

    def _next_seq(self, sid: str, base_seq: int | None = None) -> int:
        """回复消息序号：会话内单调小递增（不超 int32）。

        - 被动回复：若已知被回复消息的全局序号 base_seq，从 base_seq+1 起，
          确保大于被回复消息序号且递增；
        - 主动发送：从 1 起递增即可（官方默认语义）。
        """
        prev = self._seq_by_sid.get(sid, 0)
        floor = int(base_seq or 0)
        nxt = max(prev + 1, floor + 1)
        self._seq_by_sid[sid] = nxt
        return nxt

    async def _send_chunked(self, inbound: dict, reply: str, passive: bool = True) -> bool:
        """分段发送。返回是否实际发送了内容（空回复返回 False）。

        平台规则：只有 @本机器人事件（GROUP_AT_MESSAGE_CREATE / C2C_MSG_RECEIVE）
        的消息能用 msg_id+event_id 被动回复；全量消息事件的 event_id 不能用于
        被动回复（40034027），必须主动发送（不带 msg_id/event_id）。
        """
        segments = split_reply(reply, self.r.get("max_chars_per_segment", 80), self.r.get("max_segments", 5))
        if not segments:
            logger.debug("回复为空，不发送")
            return False
        target = inbound.get("target") or self._target_of(inbound.get("session_id") or "")[0]
        scene = inbound.get("scene") or self._target_of(inbound.get("session_id") or "")[1]
        can_passive = bool(passive and inbound.get("passive_ok"))
        gap = float(self.r.get("segment_gap_sec", 2))
        msg_id = inbound.get("msg_id") if can_passive else None
        event_id = inbound.get("event_id") if can_passive else None
        sid = inbound.get("session_id") or ""
        base_seq = inbound.get("msg_seq")
        for i, seg in enumerate(segments):
            seq = self._next_seq(sid, base_seq)
            logger.info("发送: %.60r", seg, extra={"session": session_label(sid)})
            # 模拟打字：按分段长度停顿，避免所有分段同一瞬间发出
            think = typing_delay_sec(seg, self.r)
            if think > 0:
                await asyncio.sleep(think)
            try:
                await self.bot.send_text(target, seg, scene, msg_id, seq, event_id)
            except Exception as e:  # noqa: BLE001
                if can_passive and msg_id:
                    # 被动回复失败（msg_id 过期 / 事件不可回复等）→ 降级主动发送，确保送达
                    logger.warning("被动回复失败，降级主动 target=%s: %s", oid8(target), e,
                                   extra={"session": session_label(sid)})
                    await self.bot.send_text(target, seg, scene, None, self._next_seq(sid, None))
                else:
                    raise
            # 每发送一条消息：会话在线时长 +renew_per_reply_sec（聊得火热）
            st = self._states.get(sid)
            if st is not None:
                st.renew()
            if i < len(segments) - 1:
                await asyncio.sleep(gap)
        self._recent_reply_ts = time.monotonic()
        return True

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
            "renew_per_reply_sec": self.p.get("renew_per_reply_sec"),
            "renew_streak_step": self.p.get("renew_streak_step", 0.5),
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
    在线：消息进 5s 抖动窗口；每成功回复一轮按连续轮数递增续期；倒计时结束下线。
    """

    def __init__(self, pm: "PresenceManager", sid: str):
        self.pm = pm
        self.sid = sid
        self.label = pm._session_label(sid)
        self.state = "offline"
        self.reason = "initial"
        self._online_deadline = 0.0
        self._renew_streak = 0
        self._last_active = time.monotonic()
        # 首次上线在离线周期内随机错开，避免所有会话整点同时上线
        self._next_online = time.monotonic() + self._offline_span() * random.uniform(0.4, 1.0)
        self.debounce: list[dict] = []
        self._debounce_task: asyncio.Task | None = None

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

    async def go_online(self, reason: str) -> None:
        if self.state == "online":
            return
        self.state = "online"
        self.reason = reason
        self._renew_streak = 0
        span = self._online_span()
        self._online_deadline = time.monotonic() + span
        self.touch()
        seen = self.pm._read_backlog_sid(self.sid)
        logger.info("上线（%s），预计在线 %.0fs，读到积压 %d 条（不回复）", reason, span, seen,
                    extra={"session": self.label})

    async def go_offline(self, reason: str) -> None:
        if self.state == "offline":
            return
        self.state = "offline"
        self.reason = reason
        self._renew_streak = 0
        wait = self._offline_span()
        self._next_online = time.monotonic() + wait
        # 清理本会话未发送的抖动批次（下线即停，避免残留任务/消息堆积）
        if self._debounce_task:
            self._debounce_task.cancel()
            try:
                await self._debounce_task
            except (asyncio.CancelledError, Exception):
                pass
            self._debounce_task = None
        self.debounce = []
        logger.info("下线（%s），%.0fs 后再次上线", reason, wait, extra={"session": self.label})

    def renew(self) -> None:
        """每成功回复一轮：在线时长按连续对话轮数递增（聊得越久越火热，封顶续期上限）。"""
        if not self._p.get("renew_on_reply", True):
            return
        self._renew_streak += 1
        base = float(self._p.get("renew_per_reply_sec", 60))
        step = max(0.0, float(self._p.get("renew_streak_step", 0.5)))
        cap = max(0.0, float(self._p.get("renew_max_sec", 600)))
        inc = jitter(base * (1 + step * (self._renew_streak - 1)), float(self._p.get("renew_jitter", 0.3)),
                     minimum=0.0)
        if cap:
            inc = min(inc, cap)
        self._online_deadline = max(self._online_deadline, time.monotonic()) + inc
        self.reason = f"reply-renew x{self._renew_streak}"
        self.touch()
