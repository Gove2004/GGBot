"""历史自动归档：窗口装满时，把窗口最旧的一段分批提炼成长期记忆。

一次事件最多消化 archive_overflow_messages 条，每批 archive_batch_messages 条：
一批一次提炼、一次写入记忆、推一次游标。游标之后的那段尾巴逐字节不动，
所以缓存前缀只在消化这一次整体失效，等新消息再攒满才消化下一轮。
归档跑在后台任务里，不阻塞本轮回复；同一会话同时只跑一个。
"""

from __future__ import annotations

import asyncio
import logging

from ggbot.util.session import session_label

from ggbot.core.presence.context import fmt_archive

logger = logging.getLogger("ggbot.presence")


class HistoryArchiver:
    def __init__(self, cfg, storage, llm, memory=None):
        self.cfg = cfg
        self.storage = storage
        self.llm = llm
        self.memory = memory
        self._tasks: dict[str, asyncio.Task] = {}

    # ---------- 配置现读 ----------

    @property
    def batch(self) -> int:
        """一批归档多少条历史：每批一次提炼、一次写入记忆。"""
        return max(0, min(256, int(self.cfg.context.get("archive_batch_messages", 64))))

    @property
    def overflow(self) -> int:
        """窗口满时一次从窗口最旧的一端消化多少条（按 batch 分批）。
        上限卡在一个批次之外，保证消化后窗口里至少还剩一批历史。"""
        n = max(0, min(4096, int(self.cfg.context.get("archive_overflow_messages", 512))))
        return min(n, max(0, self.history_limit - self.batch))

    @property
    def history_limit(self) -> int:
        return int(self.cfg.context.get("max_history_messages", 1024))

    @property
    def enabled(self) -> bool:
        return (
            bool(self.cfg.context.get("archive_enabled", True))
            and self.memory is not None
            and self.batch > 0
            and self.overflow > 0
        )

    # ---------- 调度 ----------

    def schedule(self, sid: str, before_id: int) -> None:
        """窗口满时调度一次后台归档；同一会话已有任务在跑就这轮跳过。"""
        if not self.enabled:
            return
        running = self._tasks.get(sid)
        if running is not None and not running.done():
            return
        cursor = self.storage.get_archive_cursor(sid)
        batch = self.storage.get_archive_batch(sid, cursor, before_id, self.batch)
        if not batch:
            return
        task = asyncio.create_task(self._drain(sid, before_id))
        self._tasks[sid] = task

        def _done(t: asyncio.Task, s: str = sid) -> None:
            if self._tasks.get(s) is t:
                self._tasks.pop(s, None)

        task.add_done_callback(_done)

    async def _drain(self, sid: str, before_id: int) -> None:
        """分批消化窗口最旧的历史：一次事件最多 overflow 条。"""
        left = self.overflow
        while left > 0:
            cursor = self.storage.get_archive_cursor(sid)
            batch = self.storage.get_archive_batch(
                sid, cursor, before_id, min(self.batch, left)
            )
            if not batch:
                return
            left -= len(batch)
            if not await self._to_memory(sid, batch):
                return  # 这一批没落地：游标不动，下一轮从同一批重试

    async def _to_memory(self, sid: str, batch: list[dict]) -> bool:
        """把一批最旧的历史交给 LLM 提炼成长期记忆；推进游标返回 True。"""
        label = session_label(sid)
        transcript = fmt_archive(batch)
        if not transcript:
            self.storage.set_archive_cursor(sid, batch[-1]["id"])
            return True
        try:
            facts = await asyncio.to_thread(self.llm.extract_memories, transcript, session=label)
        except Exception as e:  # noqa: BLE001
            logger.warning("历史归档提炼失败 n=%d: %s", len(batch), e, extra={"session": label})
            return False
        if not facts:
            # 模型判这一段没有值得记的：也算消化过了，游标照常推进
            self.storage.set_archive_cursor(sid, batch[-1]["id"])
            return True
        try:
            out = await asyncio.to_thread(self.memory.commit, facts, sid)
        except Exception as e:  # noqa: BLE001
            logger.warning("历史归档写入记忆失败: %s", e, extra={"session": label})
            return False
        self.storage.set_archive_cursor(sid, batch[-1]["id"])
        logger.info("历史归档 %d 条消息 → 记忆 新增 %d · 改写 %d · 作废 %d",
                    len(batch), out["written"], out["updated"], out["retired"],
                    extra={"session": label})
        return True

    async def cancel_all(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()
