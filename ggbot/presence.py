"""Presence 状态机：离线/在线作息、唤醒、积压分流、抖动聚合、选择性回复。

离线：普通消息仅入库；被@立即上线并立即回复该条；定时器到期上线。
在线：消息进入 debounce 窗口聚合 → LLM 选择性回复（非每条必回）；回复重置在线计时器（聊得火热）。
积压：上线后扫描 pending（≤max_scan_messages），优先级（@/提问）单独回，其余每 ≤batch_size 条合并为一条。
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from datetime import datetime

from ggbot.tools import NO_REPLY, agent_loop

logger = logging.getLogger("ggbot.presence")

SYSTEM_TEMPLATE = """你是群聊里的一个真实的人，昵称“{name}”，正在和一群朋友聊天。

【内核人设】（owner 定义，只读）
{core}

【动态人设】（你可以通过 persona_update 工具进化的部分）
{dynamic}

【长期记忆】（与当前话题相关的过往记忆）
{memory_digest}

【激活规则】（必须遵守）
{rules}

【行为准则】
1. 用口语化短句聊天，一条消息别超过 40 字；需要说多句时用空行隔开（会逐条发送）。
2. 不要每条消息都回：只有值得接话时才回复；觉得没必要回就只输出 {no_reply}。
3. 被@时必须回应。
4. 群友发来的图片/语音/视频，其下载地址会标注在消息里，你可以转发（send_image 等工具）或点评。
5. 想不起的事用 memory_search 回忆；值得长期记住的用 memory_write。
6. 说话风格、人设随相处自然进化，必要时用 persona_update 微调自己。
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
    """把一条或多条入站消息格式化为模型输入。"""
    lines = []
    for m in msgs:
        parts = [f"[{m['sender']}]"]
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

    def __init__(self, cfg, storage, llm, executor, bot):
        self.cfg = cfg
        self.storage = storage
        self.llm = llm
        self.executor = executor
        self.bot = bot

        self.p = cfg.presence
        self.r = cfg.reply
        self.b = cfg.backlog
        self._name = cfg.llm.get("bot_name", "机器人")
        ctx_cfg = cfg.context
        self._max_input = int(ctx_cfg.get("max_input_tokens", 128000))
        self._budget_tokens = int(ctx_cfg.get("summary_threshold_tokens", 100000))
        self._history_limit = int(ctx_cfg.get("max_history_messages", 50))

        self.state = "offline"
        self._online_deadline = 0.0
        self._next_online = 0.0
        self._loop_task: asyncio.Task | None = None
        self._debounce: list[dict] = []
        self._debounce_task: asyncio.Task | None = None
        self._lock = threading.RLock()
        self._recent_reply_ts = 0.0
        # 回复消息序号：小整数递增（与 msg_id 联合防重；官方默认 1）。
        # 切勿用毫秒时间戳等 int32 超限值——QQ 服务端会 400「请求数据异常」
        self._seq_by_sid: dict[str, int] = {}  # 用于面板展示

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        self._next_online = time.monotonic() + float(self.p.get("offline_interval_sec", 900))
        self._loop_task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        for t in (self._loop_task, self._debounce_task):
            if t:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass

    async def _loop(self) -> None:
        while True:
            if self.state == "offline":
                wait = self._next_online - time.monotonic()
                if wait <= 0:
                    await self._go_online("scheduled")
                else:
                    await asyncio.sleep(min(wait, 5))
            else:
                remain = self._online_deadline - time.monotonic()
                if remain <= 0:
                    await self._go_offline("timeout")
                else:
                    await asyncio.sleep(min(remain, 5))

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
        if self.state == "offline":
            if inbound.get("is_at") and self.p.get("wake_on_at_message", True):
                logger.info("收到@消息，唤醒上线并立即回复: %s", inbound["msg_id"])
                # 先即时被动回复该条（5 分钟窗口内有效、不耗主动消息额度），
                # 成功后状态标记 replied，之后处理积压不会再重复回复它
                await self._reply_now(inbound)
                await self._go_online("wake-on-at")
                return True
            logger.debug("离线：消息入库 %s (pending)", inbound["msg_id"])
            return True
        # 在线：进入 debounce 窗口聚合
        self._debounce.append(inbound)
        logger.debug("在线：进入抖动窗口 %s（当前 %d 条）", inbound["msg_id"], len(self._debounce))
        if self._debounce_task is None or self._debounce_task.done():
            self._debounce_task = asyncio.create_task(self._flush_debounce())
        return True

    async def _flush_debounce(self) -> None:
        await asyncio.sleep(float(self.r.get("debounce_sec", 10)))
        batch = self._debounce
        self._debounce = []
        if not batch:
            return
        await self._process_batch(batch)

    # ---------- 在线/离线 ----------

    async def _go_online(self, reason: str) -> None:
        if self.state == "online":
            return
        self.state = "online"
        self._online_deadline = time.monotonic() + float(self.p.get("online_duration_sec", 300))
        logger.info("上线（%s），在线时长 %.0fs，先处理积压", reason, self.p.get("online_duration_sec", 300))
        await self._process_backlog()

    async def _go_offline(self, reason: str) -> None:
        if self.state == "offline":
            return
        self.state = "offline"
        self._next_online = time.monotonic() + float(self.p.get("offline_interval_sec", 900))
        logger.info("下线（%s），%.0fs 后再次上线", reason, self.p.get("offline_interval_sec", 900))

    def _renew_online(self) -> None:
        if self.p.get("renew_on_reply", True):
            self._online_deadline = time.monotonic() + float(self.p.get("online_duration_sec", 300))

    # ---------- 回复核心 ----------

    def _target_of(self, sid: str) -> tuple[str, str]:
        """会话发送目标：优先反查 sessions.key 里的完整 openid，失败回退旧推导。

        session_id 是内部主键（uuid 片段），不能当 openid 发送（会导致 11255）。
        """
        t = self.storage.session_target(sid)
        if t and t[0]:
            return t
        return _derive_target(sid)

    async def _safe_reply(self, msgs: list[dict], passive: bool) -> bool:
        """生成并发送回复；任何失败只标记状态，不向上抛（避免单条失效群打崩整轮积压）。

        状态：成功 replied / 生成失败或发送失败 failed / 无需回复 skipped。
        发送通道：batch 中存在可被动消息（@/C2C 事件）时，绑定其中最后一条走被动回复
        （msg_id+event_id，不耗主动额度）；纯普通消息才走主动合并（额度每月 4 条/群）。
        """
        ctx_msg = msgs[-1]
        sid = ctx_msg["session_id"]
        ids = [m["msg_id"] for m in msgs if m.get("msg_id")]
        pv = [m for m in msgs if m.get("passive_ok")]
        send_msg = pv[-1] if (passive and pv) else ctx_msg
        can_passive = bool(passive and pv)

        def _mark(status: str) -> None:
            self.storage.mark_pending_processed(ids, status)

        try:
            reply = await self._agent_loop(sid, msgs, ctx_msg)
        except Exception as e:  # noqa: BLE001
            logger.warning("回复生成失败 n=%d sid=%s: %s", len(msgs), sid, e)
            _mark("failed")
            return False
        if not reply or reply == NO_REPLY:
            _mark("skipped")
            return False
        try:
            sent = await self._send_chunked(send_msg, reply, passive=can_passive)
            if not sent:
                # 回复文本切分后为空（如全空白）：视为无需发送
                _mark("skipped")
                return False
            _mark("replied")
            return True
        except Exception as e:  # noqa: BLE001
            target = send_msg.get("target") or self._target_of(sid)[0]
            scene = send_msg.get("scene") or self._target_of(sid)[1]
            logger.warning("回复发送失败 n=%d target=%s scene=%s: %s", len(msgs), target, scene, e)
            _mark("failed")
            return False

    async def _reply_now(self, inbound: dict) -> None:
        """唤醒即时回复：单条消息直接走 agent 循环。"""
        if await self._safe_reply([inbound], passive=True):
            self._renew_online()

    async def _process_batch(self, msgs: list[dict]) -> None:
        """在线批次：聚合多条消息一次决策。

        平台硬限制：全量消息不能被动回复，主动消息每月仅 4 条/群——
        在线聚合的普通消息只进上下文/记忆，不实际发送（真人"看到没回"也符合人设）；
        含被动事件（@/C2C）的批次才走回复。
        """
        last = msgs[-1]
        if not any(m.get("passive_ok") for m in msgs):
            logger.debug("在线普通消息批次跳过回复（主动额度限制）: %d 条", len(msgs))
            return
        if await self._safe_reply(msgs, passive=True):
            self._renew_online()

    async def _process_backlog(self) -> None:
        """上线积压处理：被动事件（@/C2C）且窗口内的单独被动回复，其余合并主动。

        平台硬限制：仅 @本机器人事件 / C2C 事件消息可用被动回复（带 msg_id+event_id）；
        全量消息（含问句、普通消息）的 event_id 不能用于被动回复（40034027），
        一律主动发送。主动消息额度：每月 4 条/群，故其余消息按 batch_size 合并省额度。
        """
        sessions = {}
        for sid in self._iter_pending_sessions():
            sessions[sid] = self._pending_of(sid)
        for sid, pend in sessions.items():
            if not pend:
                continue
            # 仅可被动事件（@/C2C）消息可被动；窗口内单独回，过窗并入主动合并
            at_msgs = [m for m in pend if m.get("passive_ok")]
            rest = [m for m in pend if not m.get("passive_ok")]
            fresh, stale = [], []
            for m in at_msgs:
                (fresh if self._is_fresh(m) else stale).append(m)
            for m in fresh:
                await self._safe_reply([m], passive=True)
            if stale:
                rest = stale + rest
            if rest:
                for i in range(0, len(rest), self.b.get("batch_size", 100)):
                    chunk = rest[i : i + self.b.get("batch_size", 100)]
                    await self._safe_reply(chunk, passive=False)

    def _is_fresh(self, m: dict, window_sec: int = 300) -> bool:
        """消息是否仍在被动回复窗口内（入库时间距今 ≤ window_sec）。"""
        try:
            t = datetime.strptime((m.get("ts") or "").strip(), "%Y-%m-%d %H:%M:%S")
            return (datetime.now() - t).total_seconds() <= window_sec
        except Exception:  # noqa: BLE001
            return False

    def _iter_pending_sessions(self) -> list[str]:
        limit = int(self.b.get("max_scan_messages", 1000))
        # 简化：从全局 pending 中按 session 分组
        with self._lock:
            rows = self.storage.get_recent_messages_global_pending(limit)
        seen = []
        for r in rows:
            if r["session_id"] not in seen:
                seen.append(r["session_id"])
        return seen

    def _pending_of(self, sid: str) -> list[dict]:
        return self.storage.get_pending_messages(sid, int(self.b.get("max_scan_messages", 1000)))

    # ---------- 上下文组装 ----------

    async def _agent_loop(self, sid: str, msgs: list[dict], ctx_msg: dict) -> str:
        system = await self._build_system(sid, msgs[-1] if msgs else ctx_msg)
        history = self.storage.get_recent_messages(sid, self._history_limit)
        history_msgs = []
        for h in history:
            if h["role"] == "user":
                history_msgs.append({"role": "user", "content": f"[{h['sender']}] {h['content']}"})
            else:
                history_msgs.append({"role": "assistant", "content": h["content"]})
        user_content = _fmt_user_messages(msgs)
        messages = [{"role": "system", "content": system}, *history_msgs, {"role": "user", "content": user_content}]

        # 上下文预算：超过 summary_threshold_tokens 用 LLM 摘要压缩历史
        budget = self._budget_tokens
        total = self.llm.estimate_tokens(system) + sum(
            self.llm.estimate_tokens(m.get("content") or "") for m in messages
        )
        logger.debug("agent 上下文组装: history=%d 估算tokens=%d 预算=%d",
                     len(history_msgs), total, budget)
        if total > budget and history:
            summary = await asyncio.to_thread(self.llm.summarize, _fmt_user_messages(history))
            self.storage.save_summary(sid, summary)
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": f"【更早的历史摘要】{summary}\n\n{user_content}"},
            ]
            logger.info("上下文超限，已压缩历史为摘要（%s tokens）", total)

        ctx = {
            "target": ctx_msg.get("target") or self._target_of(sid)[0],
            "scene": ctx_msg.get("scene") or self._target_of(sid)[1],
            "msg_id": ctx_msg.get("msg_id"),
            "msg_seq": 1,
            "session_id": sid,
        }
        return await agent_loop(self.llm, self.executor, messages, ctx)

    async def _build_system(self, sid: str, ctx_msg: dict) -> str:
        core = self.storage.get_persona("inner") or "（无内核人设）"
        dynamic = self.storage.get_persona("dynamic") or "（暂无）"
        rules = self.storage.list_rules(active_only=True)
        rules_text = "\n".join(f"- {r['content']}" for r in rules) or "（无）"
        # 记忆注入：用当前消息作为检索 query
        query = (ctx_msg.get("content") or "").strip()
        digest = "（无）"
        if query:
            try:
                hits = await asyncio.to_thread(self.memory.search, query, 4)
                digest = "\n".join(f"- {h['content']}" for h in hits) or "（无）"
            except Exception:  # noqa: BLE001
                digest = "（检索失败）"
        return SYSTEM_TEMPLATE.format(
            name=self._name, core=core, dynamic=dynamic,
            memory_digest=digest, rules=rules_text, no_reply=NO_REPLY,
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
            logger.info("回复发送 target=%s scene=%s 段数=%d passive=%s 段%d: %.60r",
                        target, scene, len(segments), can_passive, i + 1, seg)
            try:
                resp = await self.bot.send_text(target, seg, scene, msg_id, seq, event_id)
                if resp is not None:
                    # botpy 解析 QQ 返回（含新消息 id）；有 id 说明平台已受理并生成消息
                    rid, ts = (resp.get("id"), resp.get("timestamp")) if isinstance(resp, dict) \
                        else (getattr(resp, "id", None), getattr(resp, "timestamp", None))
                    logger.info("QQ 已受理 seq=%d resp_id=%s ts=%s", seq, rid, ts)
            except Exception as e:  # noqa: BLE001
                if can_passive and msg_id:
                    # 被动回复失败（msg_id 过期 / 事件不可回复等）→ 降级主动发送，确保送达
                    logger.warning("被动回复失败，降级主动发送 %s: %s", target, e)
                    resp = await self.bot.send_text(target, seg, scene, None, self._next_seq(sid, None))
                    if resp is not None:
                        rid, ts = (resp.get("id"), resp.get("timestamp")) if isinstance(resp, dict) \
                            else (getattr(resp, "id", None), getattr(resp, "timestamp", None))
                        logger.info("QQ 已受理(降级主动) seq=%d resp_id=%s ts=%s", seq, rid, ts)
                else:
                    raise
            if i < len(segments) - 1:
                await asyncio.sleep(gap)
        self._recent_reply_ts = time.monotonic()
        return True

    # ---------- 面板状态 ----------

    def status(self) -> dict:
        now = time.monotonic()
        if self.state == "online":
            remaining = max(0.0, self._online_deadline - now)
            next_event = "离线倒计时"
        else:
            remaining = max(0.0, self._next_online - now)
            next_event = "上线倒计时"
        return {
            "state": self.state,
            "next_event": next_event,
            "remaining_sec": round(remaining, 1),
            "pending_count": self.storage.count_pending(),
            "debounce_count": len(self._debounce),
            "online_duration_sec": self.p.get("online_duration_sec"),
            "offline_interval_sec": self.p.get("offline_interval_sec"),
            "renew_on_reply": self.p.get("renew_on_reply"),
            "debounce_sec": self.r.get("debounce_sec"),
            "backlog_max_scan": self.b.get("max_scan_messages"),
            "backlog_batch_size": self.b.get("batch_size"),
        }
