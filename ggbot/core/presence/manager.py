"""Presence 管理器：把四段职责接成一条链，并对外给出面板看得懂的那份状态。

装配关系（各段只依赖自己那一份知识，单独可测）：
    网关 ──handle_message──▶ InboundOrchestrator（入站分流 + 在线窗口聚合）
                                  │  ── 用 ──▶ SessionState（作息：什么时候上线、在线多久）
                                  ▼
                              TurnDecider（说不说、怎么说、说完记什么账）
                                  │  ── 拼上下文 ──▶ ContextBuilder
                                  │  ── 满窗归档 ──▶ HistoryArchiver
                                  ▼
                                  Outbox（被动锚点 / 主动频控 / 发送回执）

这里只留生命周期与聚合展示：2 秒一次的 tick 翻作息，start/stop 收线程，
status() 是 Web 面板要的那份快照（每秒一次的 GET /api/shell）。
"""

from __future__ import annotations

import asyncio

from ggbot.core.auth import Permissions
from ggbot.core.outbox import Outbox

from ggbot.core.presence.archiver import HistoryArchiver
from ggbot.core.presence.context import ContextBuilder
from ggbot.core.presence.decision import TurnDecider
from ggbot.core.presence.inbound import InboundOrchestrator

TICK_SEC = 2.0
"""作息扫描间隔：上下线倒计时最粗的粒度，也让面板改完配置很快生效。"""


class PresenceManager:
    """在线/离线调度 + 消息处理主循环。"""

    def __init__(self, cfg, storage, llm, executor, bot, memory=None):
        self.cfg = cfg
        self.storage = storage
        self.llm = llm
        self.executor = executor
        self.bot = bot
        self.memory = memory

        self.perms = Permissions(cfg, storage)
        self.outbox: Outbox = executor.outbox
        self.context = ContextBuilder(cfg, storage, llm, memory)
        self.archiver = HistoryArchiver(cfg, storage, llm, memory)
        self.decision = TurnDecider(storage, llm, self.perms, self.context,
                                    self.archiver, executor, self.outbox)
        self.inbound = InboundOrchestrator(cfg, storage, self.perms, self.decision)

        self._loop_task: asyncio.Task | None = None

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
            self._loop_task = None
        await self.inbound.stop()
        await self.archiver.cancel_all()

    async def _loop(self) -> None:
        while True:
            await self.inbound.tick()
            await asyncio.sleep(TICK_SEC)

    # ---------- 对外入口 ----------

    async def handle_message(self, inbound: dict) -> bool:
        """处理一条入站消息。返回 True 表示消息已被机器人消费。"""
        return await self.inbound.handle_message(inbound)

    async def speak(self, sid: str, text: str) -> bool:
        """不经模型往会话里说一句话（提醒到点就走这条通道）。"""
        return await self.inbound.speak(sid, text)

    async def forget_session(self, sid: str) -> None:
        """丢弃会话运行态（清空上下文/重置后调用），不影响其他会话。"""
        await self.inbound.forget_session(sid)

    # ---------- 面板状态 ----------

    def status(self) -> dict:
        """聚合各会话作息与现读配置，供 Web 面板展示。"""
        rows = self.inbound.panel_rows()
        online = sum(1 for s in rows if s["state"] == "online")
        # 顶层聚合字段：优先第一个在线会话的倒计时，否则第一个会话
        head = next((s for s in rows if s["state"] == "online"), rows[0] if rows else None)
        p = self.cfg.presence
        r = self.cfg.reply
        return {
            "state": "online" if online else "offline",  # 任一在线即 online
            "online_sessions": online,
            "sessions": rows,
            "next_event": head["next_event"] if head else "—",
            "remaining_sec": head["remaining_sec"] if head else 0,
            "debounce_count": sum(s["debounce_count"] for s in rows),
            "waiting_count": self.storage.count_backlog(),
            "online_duration_sec": p.get("online_duration_sec"),
            "offline_interval_sec": p.get("offline_interval_sec"),
            "online_duration_jitter": p.get("online_duration_jitter", 0.3),
            "offline_interval_jitter": p.get("offline_interval_jitter", 0.4),
            "renew_on_reply": p.get("renew_on_reply"),
            "renew_per_reply_sec": p.get("renew_per_reply_sec", 30),
            "renew_max_sec": p.get("renew_max_sec", 600),
            "renew_jitter": p.get("renew_jitter", 0.3),
            "debounce_sec": r.get("debounce_sec"),
            "backlog_max_scan": self.inbound.backlog_limit,
            "online_backlog_max": self.inbound.online_limit,
            "history_limit": self.context.history_limit,
            "archive_batch": self.archiver.batch,
            "archive_overflow": self.archiver.overflow,
            "archive_enabled": self.archiver.enabled,
            "default_level": self.perms.default_level,
            "token_budget": self.context.budget_tokens,
        }
